from pathlib import Path

import pytest

from tl_loop.preflight import PreflightError, run_preflight
from tl_loop.state.store import RunStore


def test_missing_capability_names_path_and_example(tmp_path: Path) -> None:
    exo = tmp_path / ".exo"
    exo.mkdir()
    for name in ("config.toml", "harness_policy.toml", "review-policy.toml"):
        (exo / name).write_text("", encoding="utf-8")
    with pytest.raises(PreflightError, match="harness_capability.toml") as error:
        run_preflight(tmp_path)
    assert "Example harness_capability.toml" in str(error.value)


def test_exit_reason_is_diagnostic_only(tmp_path: Path) -> None:
    store = RunStore("root", tmp_path / ".exo" / "tl-loop")
    store.record_exit_reason("capability file is missing")
    assert store.exit_reason() == "capability file is missing"
    assert not store.path.exists()
