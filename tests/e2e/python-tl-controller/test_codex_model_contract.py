"""The fixtures must provision a model the account can actually run.

On 2026-09-30 a live run of `python-tl-worker-notify` stalled for 600s and read
as a `notify_parent` defect. The worker's own rollout said otherwise: nine
events, 5.4 seconds, ending in `task_complete` with

    400 invalid_request_error: The 'gpt-luna' model is not supported when using
    Codex with a ChatGPT account.

and zero tool calls, so `notify_parent` was never attempted. The model name came
from `exomonad new`'s scaffold, which the fixtures copied into their harness
policy; the controller splits the policy key `codex/<model>` and writes the model
half into the generated child config.

These tests pin the two halves of the fix: the policy key is resolved from the
account rather than hard-coded, and the resolution refuses the name the account
rejects instead of provisioning a worker that cannot take a turn.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
E2E_DIR = PROJECT_ROOT / "tests" / "e2e"
LIB = E2E_DIR / "lib" / "python-tl.sh"

SCENARIOS = ("codex-messaging", "chainlink-codex", "python-tl-worker-notify")

#: The migrated scenarios whose `run.sh` calls the probe before `init`.
#:
#: Model *resolution* is shared -- every scenario reaches the account-resolved
#: model through `e2e_python_tl_write_harness_policy`, so no harness provisions
#: the rejected name any more. Wiring the *probe* is per-scenario work owned by
#: each migration slice, so a scenario's name moves from `PENDING_PROBE` to
#: `PROBE_WIRED` when its own slice lands the call.
#:
#: Naming the pending scenario keeps the gap visible instead of quietly dropping
#: it out of the parametrization: `test_the_probe_coverage_is_accounted_for`
#: fails unless the two lists are an exact partition of `SCENARIOS`.
PROBE_WIRED = ("codex-messaging", "chainlink-codex")
PENDING_PROBE = ("python-tl-worker-notify",)

#: The model in `exomonad new`'s scaffold. A ChatGPT-account Codex login
#: rejects it, so a fixture that provisions it produces a worker that never takes
#: a turn.
SCAFFOLD_MODEL = "gpt-luna"


def lib_source() -> str:
    return LIB.read_text(encoding="utf-8")


def resolve_model(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run `e2e_python_tl_codex_model` in a subshell and capture its verdict."""
    return subprocess.run(
        ["bash", "-c", f'source "{LIB}" >/dev/null 2>&1; e2e_python_tl_codex_model'],
        capture_output=True,
        text=True,
        env={**env, "PATH": env.get("PATH", "/usr/bin:/bin")},
    )


# --------------------------------------------------------------------------
# The policy key is resolved, not hard-coded
# --------------------------------------------------------------------------


def test_the_policy_writer_does_not_hard_code_a_model() -> None:
    """A hard-coded key provisions a model the account may not be able to run."""
    lib = lib_source()
    body = lib.split("e2e_python_tl_write_harness_policy()", 1)[1].split("\n}\n", 1)[0]
    # Only emitted lines count. The body is allowed to *name* the scaffold model
    # in a comment explaining why it cannot be used here.
    code = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )
    assert SCAFFOLD_MODEL not in code, (
        f"e2e_python_tl_write_harness_policy must not hard-code '{SCAFFOLD_MODEL}'. "
        f"Resolve the model from the account instead -- see e2e_python_tl_codex_model."
    )
    assert "e2e_python_tl_harness" in body, (
        "the policy writer must resolve its harness key through "
        "e2e_python_tl_harness so the fixtures follow the account"
    )
    # Every harness entry in all three role tables uses the resolved key.
    for role in ("tl", "worker", "reviewer"):
        section = body.split(f"[roles.{role}]", 1)[1].split("\n[", 1)[0]
        assert 'allow = ["$harness"]' in section, (
            f"roles.{role} must allow the resolved harness, not a literal model"
        )
        assert SCAFFOLD_MODEL not in section


def test_no_scenario_scaffolds_the_unrunnable_model() -> None:
    """No migrated harness may reintroduce the scaffold model by hand."""
    offenders = [
        str(path.relative_to(PROJECT_ROOT))
        for scenario in SCENARIOS
        for path in (E2E_DIR / scenario / "run.sh", LIB)
        if f'"{SCAFFOLD_MODEL}"' in path.read_text(encoding="utf-8")
        and "e2e_python_tl_codex_model" not in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"scenarios that pin the unrunnable model: {offenders}"


# --------------------------------------------------------------------------
# Resolution behaviour
# --------------------------------------------------------------------------


def test_resolution_prefers_an_explicit_override(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('model = "from-host-config"\n', encoding="utf-8")
    result = resolve_model(
        {
            "E2E_CODEX_MODEL": "explicit-model",
            "CODEX_HOST_CONFIG": str(config),
            "HOME": str(tmp_path),
        }
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "explicit-model"


def test_resolution_falls_back_to_the_host_config(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        'model_reasoning_effort = "xhigh"\nmodel = "host-model"\n', encoding="utf-8"
    )
    result = resolve_model({"CODEX_HOST_CONFIG": str(config), "HOME": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "host-model", (
        "with no override the model must come from the host Codex config -- the "
        "same place the operator's own working codex invocation gets its model"
    )


def test_resolution_refuses_the_scaffold_model(tmp_path: Path) -> None:
    """Provisioning the rejected name is the bug; refuse it at resolution."""
    result = resolve_model({"E2E_CODEX_MODEL": SCAFFOLD_MODEL, "HOME": str(tmp_path)})
    assert result.returncode != 0, (
        f"resolving '{SCAFFOLD_MODEL}' must fail: a ChatGPT-account login rejects "
        f"it with a 400 before the worker's first inference"
    )
    assert SCAFFOLD_MODEL in result.stderr
    assert "1149" in result.stderr, "the failure must point at the filed issue"


def test_resolution_fails_rather_than_guessing(tmp_path: Path) -> None:
    """No host config and no override is an error, not a default."""
    result = resolve_model(
        {"CODEX_HOST_CONFIG": str(tmp_path / "absent.toml"), "HOME": str(tmp_path)}
    )
    assert result.returncode != 0
    assert "E2E_CODEX_MODEL" in result.stderr, (
        "the failure must tell the operator how to supply a model"
    )


def test_resolution_fails_when_the_host_config_sets_no_model(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("# nothing but a comment\n", encoding="utf-8")
    result = resolve_model({"CODEX_HOST_CONFIG": str(config), "HOME": str(tmp_path)})
    assert result.returncode != 0
    assert "no top-level model" in result.stderr or "sets no" in result.stderr


# --------------------------------------------------------------------------
# The probe, so a bad model fails in seconds instead of stalling
# --------------------------------------------------------------------------


def test_the_probe_helper_exists_and_is_wired() -> None:
    lib = lib_source()
    assert "e2e_python_tl_assert_codex_model_runnable()" in lib
    # It must detect the account's rejection by its message, not by exit code
    # alone: codex prints the 400 to stderr and still exits 0 in some paths.
    assert "not supported when using Codex" in lib, (
        "the probe must match the account's rejection message; an exit-status "
        "check alone can miss it"
    )
    for scenario in PROBE_WIRED:
        run = (E2E_DIR / scenario / "run.sh").read_text(encoding="utf-8")
        assert "e2e_python_tl_assert_codex_model_runnable" in run, (
            f"{scenario}/run.sh is recorded as probe-wired, so it must probe the "
            f"model before init"
        )


def test_the_probe_coverage_is_accounted_for() -> None:
    """Every migrated scenario is either wired or explicitly still pending.

    Without this, a scenario could be dropped from the parametrization to make a
    red test go green and the only trace would be a shorter tuple. The end state
    of the migration is `PENDING_PROBE` empty, and this test is what says so.
    """
    wired = set(PROBE_WIRED)
    pending = set(PENDING_PROBE)
    assert not wired & pending, f"a scenario cannot be both: {wired & pending}"
    assert wired | pending == set(SCENARIOS), (
        f"PROBE_WIRED {sorted(wired)} plus PENDING_PROBE {sorted(pending)} must be "
        f"an exact partition of SCENARIOS {sorted(SCENARIOS)}; a scenario may not "
        f"be dropped from both lists to silence a failure"
    )


@pytest.mark.parametrize("scenario", PROBE_WIRED)
def test_the_probe_runs_before_init(scenario: str) -> None:
    """A probe after `init` has already paid for a dispatched worker."""
    text = (E2E_DIR / scenario / "run.sh").read_text(encoding="utf-8")
    probe = next(
        index
        for index, line in enumerate(text.splitlines(), 1)
        if "e2e_python_tl_assert_codex_model_runnable" in line and not line.strip().startswith("#")
    )
    init = next(
        index
        for index, line in enumerate(text.splitlines(), 1)
        if re.match(r"\s*\"?\$EXOMONAD_BIN\"?\s+init", line)
    )
    assert probe < init, (
        f"{scenario}/run.sh probes the model at line {probe} but runs init at "
        f"line {init}"
    )


@pytest.mark.skipif(
    shutil.which("codex") is None, reason="codex CLI is not installed on this host"
)
def test_the_probe_accepts_the_resolved_model(tmp_path: Path) -> None:
    """The resolved model must actually run, or the fixtures cannot work at all.

    This is the only check that would have caught the 400 in seconds rather than
    after a 600s stall, so it runs the real CLI against the real account, in an
    isolated Codex home carrying the host's auth exactly as the harness does.
    """
    host = Path.home() / ".codex" / "config.toml"
    if not host.is_file():
        pytest.skip("no host Codex config to resolve a supported model from")
    model = subprocess.run(
        ["bash", "-c", f'source "{LIB}" >/dev/null 2>&1; e2e_python_tl_codex_model'],
        capture_output=True,
        text=True,
    )
    if model.returncode != 0:
        pytest.skip(f"cannot resolve a model on this host: {model.stderr.strip()}")

    # Mirror `e2e_isolate_codex_home` + `e2e_copy_codex_auth`: an empty home plus
    # the host's auth, so the probe authenticates the same way the run will and
    # cannot edit the operator's own Codex config.
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    auth = Path.home() / ".codex" / "auth.json"
    if not auth.is_file():
        pytest.skip("no host Codex auth to authenticate the probe with")
    shutil.copy2(auth, codex_home / "auth.json")

    result = subprocess.run(
        [
            "bash", "-c",
            f'source "{LIB}" >/dev/null 2>&1; '
            f'e2e_python_tl_assert_codex_model_runnable "{model.stdout.strip()}" "$PWD"',
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**os.environ, "CODEX_HOME": str(codex_home)},
    )
    assert result.returncode == 0, (
        f"the account cannot run the model the fixtures would provision "
        f"({model.stdout.strip()}): {result.stderr.strip()}"
    )


def test_harness_propagates_a_failed_resolution(tmp_path: Path) -> None:
    """`e2e_python_tl_harness` must fail when the model cannot be resolved.

    It composes the resolver with `printf 'codex/%s'`. `printf` exits 0 on an
    empty argument, so a resolver that fails still produced `codex/` and a
    caller checking `|| return 1` saw success. The policy was then written with
    `allow = ["codex/"]` and the run failed downstream on a harness key nothing
    can dispatch -- a confusing symptom instead of the resolver's own message.
    """
    result = subprocess.run(
        ["bash", "-c", f'source "{LIB}" >/dev/null 2>&1; e2e_python_tl_harness'],
        capture_output=True,
        text=True,
        env={
            "CODEX_HOST_CONFIG": str(tmp_path / "absent.toml"),
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
        },
    )
    assert result.returncode != 0, (
        "e2e_python_tl_harness must exit non-zero when the model cannot be "
        f"resolved, so the policy writer refuses to write. It returned 0 with "
        f"stdout {result.stdout.strip()!r}"
    )
    assert "codex/" not in result.stdout, (
        "a failed resolution must not yield a bare 'codex/' harness key"
    )


def test_the_policy_writer_refuses_to_write_an_unresolvable_harness(
    tmp_path: Path,
) -> None:
    """Fail closed: no half-written policy when the harness cannot be resolved."""
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps({"run_id": "root", "budgets": {"tokens": 1000}}), encoding="utf-8"
    )
    result = subprocess.run(
        [
            "bash", "-c",
            f'source "{LIB}" >/dev/null 2>&1; '
            f'CODEX_HOST_CONFIG="{tmp_path}/absent.toml" '
            f'e2e_python_tl_write_harness_policy "{repo}" "{plan}"',
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path)},
    )
    assert result.returncode != 0, (
        "the policy writer must fail rather than emit a policy keyed on an "
        "unresolved harness"
    )
    for name in ("harness_policy.toml", "harness_capability.toml"):
        assert not (repo / ".exo" / name).exists(), (
            f"{name} must not be written when the harness cannot be resolved; a "
            f"half-written pair is what this guards against"
        )


def test_the_capability_map_covers_the_resolved_harness() -> None:
    """Policy and capability map must agree, or preflight rejects the run.

    `_require_policy_coverage` (tl_loop/select/capability.py) requires every
    harness a role allows to have a capability rating. The `exomonad new`
    scaffold ships both files keyed on `codex/gpt-luna`, so a fixture that
    rewrites only the policy fails with `missing capability entry for
    codex/<model>`. Both writes belong in the same function so they cannot
    half-apply.
    """
    lib = lib_source()
    body = lib.split("e2e_python_tl_write_harness_policy()", 1)[1].split("\n}\n", 1)[0]

    assert "harness_capability.toml" in body, (
        "the policy writer must also write the capability map the controller "
        "requires for every harness the policy allows"
    )
    section = body.split("[capabilities]", 1)[1].split("\nEOF", 1)[0]
    code = "\n".join(l for l in section.splitlines() if not l.strip().startswith("#"))
    assert '"$harness" = "standard"' in code, (
        "the capability entry must use the same resolved harness key as the policy"
    )
    assert SCAFFOLD_MODEL not in code, (
        "the capability map must not keep rating the model the account rejects"
    )


def test_the_two_files_are_written_by_one_function() -> None:
    """Two separate writers is how a fixture ends up with half the pair updated."""
    lib = lib_source()
    policy_body = lib.split("e2e_python_tl_write_harness_policy()", 1)[1].split("\n}\n", 1)[0]
    assert "harness_policy.toml" in policy_body
    assert "harness_capability.toml" in policy_body, (
        "both files must be written by e2e_python_tl_write_harness_policy; a "
        "separate capability writer is a second thing to remember to call"
    )
