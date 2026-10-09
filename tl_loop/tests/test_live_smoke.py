from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tl_loop.rlm.call import RlmCallError
from tl_loop.rlm.live_smoke import (
    _OPENCODE_FREE_MODEL_GROUPS,
    configured_models,
    installed_harnesses,
    main,
    model_environment_name,
    opencode_model_groups,
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


def test_opencode_defaults_to_free_zen_and_go_model_catalogs() -> None:
    models = configured_models(("opencode",), {})["opencode"]

    assert opencode_model_groups(models) == tuple(
        (provider, model_ids) for provider, _prefix, model_ids in _OPENCODE_FREE_MODEL_GROUPS
    )


def test_opencode_model_override_rejects_paid_models() -> None:
    environment = {model_environment_name("opencode"): "opencode-go/gpt-6-luna"}

    with pytest.raises(ValueError, match="only accepts published free"):
        configured_models(("opencode",), environment)


def test_opencode_model_override_keeps_catalog_order_and_groups_providers() -> None:
    selected_models = (
        "opencode/space-bunny-free, opencode/big-pickle, opencode-go/step-5-preview-free"
    )
    environment = {model_environment_name("opencode"): selected_models}

    assert opencode_model_groups(configured_models(("opencode",), environment)["opencode"]) == (
        (
            "Zen",
            ("opencode/space-bunny-free", "opencode/big-pickle"),
        ),
        ("Go", ("opencode-go/step-5-preview-free",)),
    )


def test_main_cycles_free_models_until_each_opencode_provider_passes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    passing_models = {
        "opencode/space-bunny-free",
        "opencode-go/step-5-preview-free",
    }
    monkeypatch.setattr("tl_loop.rlm.live_smoke.installed_harnesses", lambda: ("opencode",))
    monkeypatch.setenv(
        model_environment_name("opencode"),
        "opencode/big-pickle,opencode/space-bunny-free,"
        "opencode-go/longcat-2.5-preview-free,opencode-go/step-5-preview-free",
    )

    def fake_run_smoke(harness: str, model_id: str) -> tuple[int, int, int]:
        assert harness == "opencode"
        calls.append(model_id)
        if model_id not in passing_models:
            raise RlmCallError("model unavailable")
        return 12, 3, 25

    monkeypatch.setattr("tl_loop.rlm.live_smoke.run_smoke", fake_run_smoke)

    assert main(("--harness", "opencode")) == 0
    assert calls == [
        "opencode/big-pickle",
        "opencode/space-bunny-free",
        "opencode-go/longcat-2.5-preview-free",
        "opencode-go/step-5-preview-free",
    ]
    assert "PASS opencode provider=Zen model=opencode/space-bunny-free" in capsys.readouterr().out


def test_smoke_request_requires_the_exact_addition_result() -> None:
    request = smoke_request("model-x")

    assert request.name == "live_cli_smoke"
    assert request.inputs == {"left": 2, "right": 3}
    assert request.output_schema["required"] == ["sum"]
    assert request.output_schema["additionalProperties"] is False


def test_opencode_backend_denies_every_tool_and_keeps_scratch_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tl_loop.rlm.subprocess_backend.shutil.which", lambda name: f"/bin/{name}")
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

    response = SubprocessRlmBackend("opencode", "provider/model", runner=runner).complete(
        smoke_request("provider/model")
    )

    command = calls[0]["command"]
    assert isinstance(command, list)
    assert command[1:5] == ["run", "--standalone", "-m", "provider/model"]
    assert "--auto" not in command
    assert response.output == {"sum": 5}


def test_codex_backend_keeps_auth_home_with_user_config_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tl_loop.rlm.subprocess_backend.shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setenv("CODEX_HOME", "/tmp/codex-account-home")
    calls: list[dict[str, object]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text('{"sum":5}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    response = SubprocessRlmBackend("codex", "gpt-6-luna", runner=runner).complete(
        smoke_request("gpt-6-luna")
    )

    command = calls[0]["command"]
    environment = calls[0]["env"]
    assert isinstance(command, list)
    assert "--ignore-user-config" in command
    assert isinstance(environment, dict)
    assert environment["CODEX_HOME"] == "/tmp/codex-account-home"
    assert response.output == {"sum": 5}
