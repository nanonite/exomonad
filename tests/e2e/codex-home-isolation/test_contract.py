"""Contract checks for the per-run Codex home shared by the E2E harnesses.

These run without a server, a tmux session, or a Codex process, so they are the
fast gate that catches a harness which has drifted out of the isolation contract
before a real run is attempted. They cover three things:

* the *helper* itself -- it creates the home under the run, exports it, copies
  only the documented auth artifacts, never touches the host config, and refuses
  a home that is not run-scoped
* the *sentinel* -- a run that seeds Codex configuration the way ExoMonad does
  leaves the host ``config.toml`` byte-for-byte identical, which is the property
  the whole contract exists to protect
* the *wiring* -- every harness that can generate Codex configuration reaches the
  shared helper, so no scenario has to remember to export ``CODEX_HOME`` and
  none of them copies, snapshots, or restores the host config
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LIB_DIR = PROJECT_ROOT / "tests" / "e2e" / "lib"
E2E_DIR = PROJECT_ROOT / "tests" / "e2e"
HELPER = LIB_DIR / "codex-home.sh"

sys.path.insert(0, str(LIB_DIR))

import e2e_harness.codex_home as codex_home  # noqa: E402

#: Mirrors the shell helper's artifact list. These two are one contract in two
#: languages, and a run that copies anything else -- ``config.toml`` above all --
#: is the failure this whole mechanism exists to prevent.
EXPECTED_AUTH_ARTIFACTS = ("auth.json", "installation_id")

#: Harness entry points that can generate Codex configuration: they configure a
#: Codex root, spawn, or reviewer agent type, and they start a real ExoMonad
#: process.
#:
#: Two groups, because two ways of reaching the contract are legitimate. A
#: harness built on ``lib/harness.sh`` inherits isolation from
#: ``e2e_create_work_dir`` and the sentinel from ``e2e_cleanup``, so it has
#: nothing of its own to arrange. A bespoke harness that manages its own work
#: dir must call the shared helper itself. What neither group may do is arrange
#: its own Codex home.
LIBRARY_HARNESSES = (
    "cross-harness-inbox/run.sh",
    "init-continue/run.sh",
    "init-recovery/run.sh",
    "one-shot-lifecycle/run.sh",
)

BESPOKE_HARNESSES = (
    "authorship/run.sh",
    "chainlink-close/run.sh",
    "chainlink-env-failsafe/run.sh",
    "chainlink-codex/run.sh",
    "chainlink/run.sh",
    "codex-messaging/run.sh",
    "codex-reviewer-sandbox/run.sh",
    "lifecycle/run.sh",
    "orphan-pr-guard/run.sh",
    "review-loop-stuck/run.sh",
    "subtl-worker-notify/run.sh",
    "tl-to-worker-messaging/run.sh",
)

#: Bespoke harnesses that start a real `codex` process, so credentials travel
#: with the isolated home. Every other Codex-generating harness uses a fixture
#: binary and copies nothing. This list and the harness list must agree:
#: ``test_only_live_codex_harnesses_copy_credentials`` checks that.
LIVE_CODEX_HARNESSES = (
    "chainlink-close",
    "chainlink-codex",
    "chainlink",
    "codex-messaging",
    "orphan-pr-guard",
    "subtl-worker-notify",
    "tl-to-worker-messaging",
)

CODEX_GENERATING_HARNESSES = LIBRARY_HARNESSES + BESPOKE_HARNESSES

#: Python acceptances that start a real server or `exomonad init` with a Codex
#: agent type. Each isolates the Codex home through the shared helper.
CODEX_GENERATING_PYTHON_HARNESSES = (
    "ordered-recursive/real_server_transport.py",
    "recreated-leaf-recovery/project.py",
    "recursive-crash-convergence/scenario.py",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The shell helper
# ---------------------------------------------------------------------------


def test_helper_exists_and_is_sourced_by_the_shared_harness() -> None:
    assert HELPER.is_file(), "tests/e2e/lib/codex-home.sh is the shared helper"
    assert "codex-home.sh" in read(LIB_DIR / "harness.sh"), (
        "harness.sh must source the helper, so every harness.sh user inherits it"
    )


def test_shared_harness_isolates_on_work_dir_creation() -> None:
    """A harness cannot forget: creating a work dir isolates Codex.

    The anti-pattern this replaces is a scenario that has to remember to export
    CODEX_HOME. Isolating inside e2e_create_work_dir makes the forgetting
    impossible for every harness that sources the library.
    """
    harness = read(LIB_DIR / "harness.sh")
    create = harness.split("e2e_create_work_dir()", 1)[1].split("\n}", 1)[0]
    assert "e2e_isolate_codex_home" in create, (
        "e2e_create_work_dir must call e2e_isolate_codex_home"
    )


def test_shared_harness_asserts_the_sentinel_at_teardown() -> None:
    cleanup = read(LIB_DIR / "harness.sh").split("e2e_cleanup()", 1)[1].split(
        "\n}", 1
    )[0]
    assert "e2e_codex_assert_home_is_run_scoped" in cleanup, (
        "teardown must re-check the host config, not trust the run kept to it"
    )
    assert "e2e_codex_remove_isolated_home" in cleanup, (
        "teardown must remove the isolated home, and only the isolated home"
    )


def test_helper_never_copies_modifies_or_restores_the_host_config() -> None:
    """The host config is read-only, and only to compute a digest.

    A copy/restore scheme is the anti-pattern named in the issue: ExoMonad
    rewrites that file in place, and a restore cannot run if the run dies.
    """
    helper = read(HELPER)
    for forbidden in (
        "cp -p \"$HOME/.codex/config.toml",
        "cp \"$HOME/.codex/config.toml",
        "mv ",
    ):
        assert forbidden not in helper, f"host config must never be moved: {forbidden}"
    # The host home is named in exactly one place, and only to read a checksum.
    assert helper.count("$HOME/.codex") == 1, (
        "the helper must resolve the host home through e2e_codex_host_home alone"
    )
    host_home_body = helper.split("e2e_codex_host_home() {", 1)[1].split("\n}", 1)[0]
    assert host_home_body.count("$HOME/.codex") == 1
    assert "e2e_codex_file_digest" in helper, "the sentinel is a digest comparison"


def test_helper_copies_only_the_documented_auth_artifacts() -> None:
    helper = read(HELPER)
    for artifact in EXPECTED_AUTH_ARTIFACTS:
        assert artifact in helper, f"{artifact} is a documented auth artifact"
    assert "config.toml" not in helper.split("E2E_CODEX_AUTH_ARTIFACTS", 1)[1].split(
        ")", 1
    )[0], "the auth artifact list must not contain config.toml"


def test_helper_is_idempotent_and_sourced_once() -> None:
    """Sourcing twice is safe, so a harness may call the helper explicitly too."""
    assert "E2E_CODEX_HOME_HELPER_LOADED" in read(HELPER)


# ---------------------------------------------------------------------------
# Behaviour, driven through a real bash
# ---------------------------------------------------------------------------


def bash(script: str, *, home: Path, work_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        text=True,
        capture_output=True,
        env={
            "HOME": str(home),
            "WORK_DIR": str(work_dir),
            "PATH": os.environ.get("PATH", ""),
        },
        check=False,
    )


@pytest.fixture
def host_home(tmp_path: Path) -> Path:
    """A host Codex home with a config that looks like a real one."""
    home = tmp_path / "host-home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        'model = "gpt-5"\n\n[hooks.state]\n"key" = { trusted = true }\n',
        encoding="utf-8",
    )
    (home / ".codex" / "auth.json").write_text('{"token":"secret"}', encoding="utf-8")
    (home / ".codex" / "installation_id").write_text("abc123", encoding="utf-8")
    return home


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    work = tmp_path / "run"
    work.mkdir()
    return work


def test_isolate_creates_and_exports_a_run_scoped_home(
    host_home: Path, work_dir: Path
) -> None:
    result = bash(
        f"source {HELPER}\n"
        "e2e_isolate_codex_home\n"
        'test "$CODEX_HOME" = "$WORK_DIR/codex-home"\n'
        'test -d "$CODEX_HOME"\n'
        "e2e_codex_assert_home_is_run_scoped\n",
        home=host_home,
        work_dir=work_dir,
    )
    assert result.returncode == 0, result.stderr


def test_isolate_leaves_the_host_config_byte_for_byte_unchanged(
    host_home: Path, work_dir: Path
) -> None:
    """The sentinel, proved the way the bug actually happened.

    A run seeds hook trust exactly the way ExoMonad does -- append to the Codex
    user config -- and the host file must come out identical.
    """
    host_config = host_home / ".codex" / "config.toml"
    before = hashlib.sha256(host_config.read_bytes()).hexdigest()

    result = bash(
        f"source {HELPER}\n"
        "e2e_isolate_codex_home\n"
        # The append ExoMonad's install_codex_hook_trust performs.
        'cat >> "$CODEX_HOME/config.toml" <<EOF\n'
        '[hooks.state]\n'
        '"/run/worktrees/leaf-codex/.codex/config.toml:pre_tool_use" = '
        "{ trusted = true }\n"
        "EOF\n"
        "e2e_codex_assert_host_config_unchanged\n"
        "e2e_codex_assert_home_is_run_scoped\n",
        home=host_home,
        work_dir=work_dir,
    )
    assert result.returncode == 0, result.stderr
    assert hashlib.sha256(host_config.read_bytes()).hexdigest() == before
    assert (work_dir / "codex-home" / "config.toml").is_file()


def test_sentinel_fails_when_the_host_config_changed(
    host_home: Path, work_dir: Path
) -> None:
    result = bash(
        f"source {HELPER}\n"
        "e2e_isolate_codex_home\n"
        "echo tampered >> \"$HOME/.codex/config.toml\"\n"
        "e2e_codex_assert_host_config_unchanged\n",
        home=host_home,
        work_dir=work_dir,
    )
    assert result.returncode != 0, "a modified host config must fail the sentinel"
    assert "changed during the run" in result.stderr


def test_copy_auth_copies_only_documented_artifacts(
    host_home: Path, work_dir: Path
) -> None:
    result = bash(
        f"source {HELPER}\n"
        "e2e_isolate_codex_home\n"
        "e2e_copy_codex_auth\n"
        'test -f "$CODEX_HOME/auth.json"\n'
        'test -f "$CODEX_HOME/installation_id"\n'
        'test ! -f "$CODEX_HOME/config.toml"\n',
        home=host_home,
        work_dir=work_dir,
    )
    assert result.returncode == 0, result.stderr


def test_isolation_refuses_a_home_outside_the_run(
    host_home: Path, work_dir: Path
) -> None:
    result = bash(
        f"source {HELPER}\n"
        "e2e_isolate_codex_home\n"
        'export CODEX_HOME="$HOME/.codex"\n'
        "e2e_codex_assert_home_is_run_scoped\n",
        home=host_home,
        work_dir=work_dir,
    )
    assert result.returncode != 0, "a host-scoped CODEX_HOME must be rejected"
    assert "not beneath WORK_DIR" in result.stderr


def test_isolation_refuses_a_home_outside_the_run_before_teardown_runs(
    host_home: Path, work_dir: Path
) -> None:
    """A harness that never isolates reports nothing rather than a false leak."""
    result = bash(
        f"source {HELPER}\n"
        "e2e_codex_assert_home_is_run_scoped\n",
        home=host_home,
        work_dir=work_dir,
    )
    assert result.returncode == 0, result.stderr


def test_keep_workdir_preserves_the_isolated_home(host_home: Path, work_dir: Path) -> None:
    """KEEP_E2E_WORKDIR=1 keeps isolated state; it is never weakened."""
    result = bash(
        f"source {HELPER}\n"
        "e2e_isolate_codex_home\n"
        'touch "$CODEX_HOME/config.toml"\n'
        'KEEP_E2E_WORKDIR=1 e2e_codex_remove_isolated_home\n'
        'test -f "$CODEX_HOME/config.toml"\n',
        home=host_home,
        work_dir=work_dir,
    )
    assert result.returncode == 0, result.stderr


def test_teardown_removes_only_the_isolated_home(
    host_home: Path, work_dir: Path
) -> None:
    result = bash(
        f"source {HELPER}\n"
        "e2e_isolate_codex_home\n"
        "e2e_codex_remove_isolated_home\n"
        'test ! -e "$CODEX_HOME"\n'
        'test -f "$HOME/.codex/config.toml"\n'
        'test -d "$WORK_DIR"\n',
        home=host_home,
        work_dir=work_dir,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# The Python helper
# ---------------------------------------------------------------------------


def test_python_helper_matches_the_shell_artifact_list() -> None:
    assert codex_home.AUTH_ARTIFACTS == EXPECTED_AUTH_ARTIFACTS
    assert "config.toml" not in codex_home.AUTH_ARTIFACTS


def test_python_isolate_is_run_scoped_and_never_copies_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text("model = 'x'\n", encoding="utf-8")
    (home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    root = tmp_path / "run"
    root.mkdir()
    environment: dict[str, str] = {}

    created = codex_home.isolate(root, environment)

    assert environment["CODEX_HOME"] == str(created)
    assert created == root / "codex-home"
    codex_home.assert_run_scoped(root, environment)
    # No live Codex process, so no credentials were copied.
    assert not (created / "auth.json").exists()
    assert (home / ".codex" / "config.toml").read_text(encoding="utf-8") == "model = 'x'\n"


def test_python_sentinel_detects_a_modified_host_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    host_config = home / ".codex" / "config.toml"
    host_config.write_text("model = 'x'\n", encoding="utf-8")
    root = tmp_path / "run"
    root.mkdir()
    environment: dict[str, str] = {}
    codex_home.isolate(root, environment, home=home)

    codex_home.assert_untouched(root, home=home)
    host_config.write_text("model = 'tampered'\n", encoding="utf-8")
    with pytest.raises(codex_home.CodexHomeError, match="changed during the run"):
        codex_home.assert_untouched(root, home=home)


def test_python_isolate_with_auth_copies_only_documented_artifacts(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    (home / ".codex" / "config.toml").write_text("model = 'x'\n", encoding="utf-8")
    root = tmp_path / "run"
    root.mkdir()
    environment: dict[str, str] = {}

    created = codex_home.isolate(root, environment, home=home, copy_auth=True)

    assert (created / "auth.json").is_file()
    assert not (created / "config.toml").exists()


def test_python_isolate_refuses_auth_when_the_host_has_none(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    root = tmp_path / "run"
    root.mkdir()
    environment: dict[str, str] = {}

    with pytest.raises(codex_home.CodexHomeError, match="cannot\n?authenticate|authenticate"):
        codex_home.isolate(root, environment, home=home, copy_auth=True)


def test_python_run_scope_rejects_a_host_home(tmp_path: Path) -> None:
    root = tmp_path / "run"
    root.mkdir()
    environment = {"CODEX_HOME": str(tmp_path / "elsewhere" / "codex-home")}
    with pytest.raises(codex_home.CodexHomeError, match="not beneath"):
        codex_home.assert_run_scoped(root, environment)


def test_python_run_scope_rejects_an_unset_home(tmp_path: Path) -> None:
    root = tmp_path / "run"
    root.mkdir()
    with pytest.raises(codex_home.CodexHomeError, match="unset"):
        codex_home.assert_run_scoped(root, {})


# ---------------------------------------------------------------------------
# The wiring: no harness arranges its own Codex home
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relative", LIBRARY_HARNESSES)
def test_library_harness_inherits_isolation_from_the_shared_harness(
    relative: str,
) -> None:
    """A scenario cannot forget: the library isolates and asserts for it.

    This is the anti-pattern the issue names -- depending on each scenario
    remembering to export CODEX_HOME. These harnesses reach the contract only
    through e2e_create_work_dir and e2e_cleanup, and deliberately carry no
    Codex logic of their own.
    """
    body = read(E2E_DIR / relative)
    assert "lib/harness.sh" in body, f"{relative} must build on the shared harness"
    assert "e2e_create_work_dir" in body
    assert "e2e_isolate_codex_home" not in body, (
        f"{relative} duplicates what e2e_create_work_dir already does"
    )
    assert "e2e_codex_assert_home_is_run_scoped" not in body, (
        f"{relative} duplicates what e2e_cleanup already does"
    )


@pytest.mark.parametrize("relative", BESPOKE_HARNESSES)
def test_bespoke_harness_reaches_the_shared_helper(relative: str) -> None:
    body = read(E2E_DIR / relative)
    assert "e2e_isolate_codex_home" in body, (
        f"{relative} can generate Codex configuration and must isolate its own "
        f"Codex home through the shared helper"
    )


@pytest.mark.parametrize("relative", CODEX_GENERATING_HARNESSES)
def test_shell_harness_does_not_arrange_its_own_codex_home(relative: str) -> None:
    """The bespoke pattern this replaces: a hand-rolled $WORK_DIR/codex-home."""
    body = read(E2E_DIR / relative)
    assert "CODEX_HOME_DIR" not in body, (
        f"{relative} still has a private CODEX_HOME_DIR; use the shared helper"
    )
    assert "codex-home" not in body.replace("codex-home.sh", ""), (
        f"{relative} still names a codex-home path directly"
    )


@pytest.mark.parametrize("relative", CODEX_GENERATING_HARNESSES)
def test_shell_harness_never_touches_the_host_codex_config(relative: str) -> None:
    body = read(E2E_DIR / relative)
    assert "$HOME/.codex/config.toml" not in body, (
        f"{relative} must not read or write the host Codex config"
    )


@pytest.mark.parametrize("relative", BESPOKE_HARNESSES)
def test_bespoke_harness_asserts_the_sentinel_at_teardown(relative: str) -> None:
    body = read(E2E_DIR / relative)
    assert "e2e_codex_assert_home_is_run_scoped" in body, (
        f"{relative} must re-check the host config during teardown"
    )
    assert "e2e_codex_remove_isolated_home" in body, (
        f"{relative} must remove the isolated home it created"
    )


@pytest.mark.parametrize("relative", BESPOKE_HARNESSES)
def test_bespoke_harness_preserves_keep_e2e_workdir(relative: str) -> None:
    """KEEP_E2E_WORKDIR is not weakened: teardown still honours it.

    The sentinel is the one thing that is not skipped when a work dir is kept --
    the host config is outside the work dir, so keeping state must not also keep
    the check.
    """
    body = read(E2E_DIR / relative)
    assert "KEEP_E2E_WORKDIR" in body, f"{relative} must honour KEEP_E2E_WORKDIR"
    teardown = body.split("cleanup()", 1)[1]
    assert teardown.index("e2e_codex_assert_home_is_run_scoped") < teardown.index(
        "KEEP_E2E_WORKDIR"
    ), f"{relative} must assert the sentinel before deciding what to keep"


@pytest.mark.parametrize("relative", CODEX_GENERATING_PYTHON_HARNESSES)
def test_python_harness_isolates_its_codex_home(relative: str) -> None:
    body = read(E2E_DIR / relative)
    assert "codex_home.isolate" in body, (
        f"{relative} starts a real server with a Codex agent type and must "
        f"isolate its Codex home"
    )
    assert "copy_auth=True" not in body, (
        f"{relative} uses a fixture codex binary and must not copy credentials"
    )


def test_python_server_transport_asserts_the_sentinel_at_teardown() -> None:
    body = read(E2E_DIR / "ordered-recursive" / "real_server_transport.py")
    assert "codex_home.assert_untouched" in body, (
        "every acceptance that stops the server must re-check the host config"
    )


def test_python_recreated_leaf_recovery_asserts_the_sentinel() -> None:
    body = read(E2E_DIR / "recreated-leaf-recovery" / "project.py")
    assert "codex_home.assert_untouched" in body, (
        "the recreated-leaf teardown must re-check the host config"
    )


#: ``serve`` and ``init`` are the subcommands that spawn agents and seed Codex
#: hook trust; ``new`` only scaffolds project files and so is not swept.
#: ``e2e_start_server`` is the shared library's spelling of the same thing, so a
#: harness that starts its server through the library is swept too.
EXOMONAD_PROCESS = re.compile(
    r'EXOMONAD_BIN" (?:serve|init)|"\$EXOMONAD_BIN" (?:serve|init)|\be2e_start_server\b'
)


def exomonad_process_harnesses() -> list[str]:
    """Return every shell harness entry point that starts a real ExoMonad process."""
    return [
        f"{path.parent.name}/run.sh"
        for path in sorted(E2E_DIR.glob("*/run.sh"))
        if EXOMONAD_PROCESS.search(read(path))
    ]


def test_every_exomonad_harness_isolates_its_codex_home() -> None:
    """Sweep, not a hand-maintained list: nothing that starts ExoMonad is exempt.

    The named lists above drive harness-specific assertions. This one is the
    exhaustive check, so a harness added later without isolation fails here
    rather than waiting for someone to notice the host config grew entries.
    """
    unaccounted = [
        relative
        for relative in exomonad_process_harnesses()
        if "e2e_isolate_codex_home" not in read(E2E_DIR / relative)
        and "lib/harness.sh" not in read(E2E_DIR / relative)
    ]
    assert not unaccounted, (
        "these harnesses start an ExoMonad process but do not isolate a Codex "
        f"home: {unaccounted}"
    )


def test_the_sweep_actually_finds_the_harnesses() -> None:
    """A sweep that silently matches nothing would pass forever."""
    swept = set(exomonad_process_harnesses())
    assert len(swept) >= 20, f"sweep found only {sorted(swept)}"
    for relative in CODEX_GENERATING_HARNESSES:
        assert relative in swept, (
            f"{relative} is listed as Codex-generating but the sweep does not "
            f"see it starting an ExoMonad process"
        )


def test_only_live_codex_harnesses_copy_credentials() -> None:
    """Credential copying is opt-in, and only where a real codex runs.

    A harness with a fixture `codex` binary must not pull real credentials into
    its work dir at all, and a harness that does start a real one that copied
    nothing would fail far from the cause.
    """
    copies = {
        path.parent.name
        for path in E2E_DIR.glob("*/run.sh")
        if "e2e_copy_codex_auth" in read(path)
    }
    assert copies == set(LIVE_CODEX_HARNESSES), (
        f"credential copying changed: copying={sorted(copies)} "
        f"live={sorted(LIVE_CODEX_HARNESSES)}"
    )
