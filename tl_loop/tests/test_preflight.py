import json
from pathlib import Path

import pytest

from tl_loop.preflight import PreflightError, capability_example, run_preflight
from tl_loop.select.policy import load_policy
from tl_loop.state.store import RunStore

ROOT = Path(__file__).parents[2]


def _project(tmp_path: Path, *, capability: str | None = "custom") -> Path:
    exo = tmp_path / ".exo"
    (exo / "tl-loop").mkdir(parents=True)
    (exo / "config.toml").write_text("", encoding="utf-8")
    (exo / "review-policy.toml").write_text(
        (ROOT / ".exo" / "review-policy.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (exo / "harness_policy.toml").write_text(
        """[roles.tl]
allow = [\"codex/custom\"]
cost_rank = { \"codex/custom\" = 1 }
token_budget = 1
escalate_after_attempts = 1

[roles.worker]
allow = [\"codex/custom\"]
cost_rank = { \"codex/custom\" = 1 }
token_budget = 1
escalate_after_attempts = 1

[roles.reviewer]
allow = [\"codex/custom\"]
cost_rank = { \"codex/custom\" = 1 }
token_budget = 1
escalate_after_attempts = 1
""",
        encoding="utf-8",
    )
    (tmp_path / ".exo" / "tl-loop" / "plan.json").write_text(
        '{"plan":{"workers":[],"leaves":[],"sub_tls":[]}}', encoding="utf-8"
    )
    if capability is not None:
        (exo / "harness_capability.toml").write_text(
            f"[capabilities]\n\"codex/custom\" = \"{capability}\"\n", encoding="utf-8"
        )
    return tmp_path


def test_policy_derived_example_can_be_copied_verbatim(tmp_path: Path) -> None:
    project = _project(tmp_path, capability=None)
    policy = load_policy(project / ".exo" / "harness_policy.toml")
    example = capability_example(policy)
    assert '"codex/custom" = "standard"' in example
    (project / ".exo" / "harness_capability.toml").write_text(example, encoding="utf-8")
    assert run_preflight(project).project_root == project.resolve()


def test_missing_capability_names_path_and_policy_derived_example(tmp_path: Path) -> None:
    project = _project(tmp_path, capability=None)
    with pytest.raises(PreflightError, match="harness_capability.toml") as error:
        run_preflight(project)
    assert '"codex/custom" = "standard"' in str(error.value)


def test_missing_and_invalid_policy_fail_preflight(tmp_path: Path) -> None:
    project = _project(tmp_path)
    policy = project / ".exo" / "harness_policy.toml"
    policy.unlink()
    with pytest.raises(PreflightError, match="harness_policy.toml"):
        run_preflight(project)
    policy.write_text("roles = [", encoding="utf-8")
    with pytest.raises(PreflightError, match="invalid"):
        run_preflight(project)


def test_missing_and_invalid_plan_fail_preflight(tmp_path: Path) -> None:
    project = _project(tmp_path)
    plan = project / ".exo" / "tl-loop" / "plan.json"
    plan.unlink()
    with pytest.raises(PreflightError, match="plan.json"):
        run_preflight(project)
    plan.write_text("{\"plan\": {\"unknown\": true}}", encoding="utf-8")
    with pytest.raises(PreflightError, match="invalid plan"):
        run_preflight(project)


def test_exit_reason_is_diagnostic_only(tmp_path: Path) -> None:
    store = RunStore("root", tmp_path / ".exo" / "tl-loop")
    store.record_exit_reason("capability file is missing")
    assert store.exit_reason() == "capability file is missing"
    assert not store.path.exists()


def test_exit_reason_preserves_chained_error_diagnostics(tmp_path: Path) -> None:
    store = RunStore("root", tmp_path / ".exo" / "tl-loop")
    cause = ValueError("segment-000000000001.jsonl line 7")
    cause.segment = Path("segment-000000000001.jsonl")  # type: ignore[attr-defined]
    cause.line_number = 7  # type: ignore[attr-defined]
    error = RuntimeError("ledger tailer stopped")
    error.__cause__ = cause
    error.cursor = 0  # type: ignore[attr-defined]
    error.sequence_status = "partial"  # type: ignore[attr-defined]

    store.record_exit_reason(str(error), error=error)

    payload = json.loads(store.exit_reason_path.read_text(encoding="utf-8"))
    assert payload["reason"] == "ledger tailer stopped"
    assert payload["error_chain"] == [
        "RuntimeError: ledger tailer stopped",
        "ValueError: segment-000000000001.jsonl line 7",
    ]
    assert payload["context"] == {
        "cursor": 0,
        "line_number": 7,
        "segment": "segment-000000000001.jsonl",
        "sequence_status": "partial",
    }
