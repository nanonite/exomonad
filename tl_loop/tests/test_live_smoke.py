from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tl_loop.rlm.live_smoke import (
    configured_models,
    installed_harnesses,
    model_environment_name,
    smoke_request,
)
from tl_loop.rlm.subprocess_backend import SubprocessRlmBackend


def test_installed_harnesses_only_includes_supported_binaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = {"codex", "opencode"}
    monkeypatch.setattr(
        "tl_loop.rlm.live_smoke.shutil.which",
        lambda harness: f"/bin/{harness}" if harness in installed else None,
    )

    assert installed_harnesses() == ("codex", "opencode")


def test_configured_models_requires_all_installed_harnesses() -> None:
    environment = {model_environment_name("claude"): "sonnet"}

    with pytest.raises(ValueError, match="EXOMONAD_TL_SMOKE_CODEX_MODEL"):
        configured_models(("claude", "codex"), environment)


def test_smoke_request_requires_the_exact_addition_result() -> None:
    request = smoke_request("model-x")

    assert request.name == "live_cli_smoke"
    assert request.inputs == {"left": 2, "right": 3}
    assert request.output_schema["required"] == ["sum"]
    assert request.output_schema["additionalProperties"] is False


def test_opencode_backend_denies_every_tool_and_keeps_scratch_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tl_loop.rlm.subprocess_backend.shutil.which", lambda name: f"/bin/{name}"
    )
    monkeypatch.setenv("OPENCODE_PERMISSION", '{"bash":"allow"}')
    calls: list[dict[str, object]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        workdir = Path(str(kwargs["cwd"]))
        assert list(workdir.iterdir()) == []
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        permission = json.loads(str(environment["OPENCODE_PERMISSION"]))
        assert permission == {"*": "deny"}
        return subprocess.CompletedProcess(command, 0, '{"sum":5}', "")

    response = SubprocessRlmBackend(
        "opencode", "provider/model", runner=runner
    ).complete(smoke_request("provider/model"))

    command = calls[0]["command"]
    assert isinstance(command, list)
    assert command[1:5] == ["run", "--standalone", "-m", "provider/model"]
    assert "--auto" not in command
    assert response.output == {"sum": 5}


def test_codex_backend_keeps_auth_home_with_user_config_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tl_loop.rlm.subprocess_backend.shutil.which", lambda name: f"/bin/{name}"
    )
    monkeypatch.setenv("CODEX_HOME", "/tmp/codex-account-home")
    calls: list[dict[str, object]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text('{"sum":5}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    response = SubprocessRlmBackend(
        "codex", "gpt-6-luna", runner=runner
    ).complete(smoke_request("gpt-6-luna"))

    command = calls[0]["command"]
    environment = calls[0]["env"]
    assert isinstance(command, list)
    assert "--ignore-user-config" in command
    assert isinstance(environment, dict)
    assert environment["CODEX_HOME"] == "/tmp/codex-account-home"
    assert response.output == {"sum": 5}
