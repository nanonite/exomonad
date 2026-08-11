"""Coverage for capability ratings and fail-closed policy coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from tl_loop.select.capability import (
    CapabilityMap,
    is_capable,
    load_capability,
    validate_capability,
)
from tl_loop.select.classify import Difficulty
from tl_loop.select.policy import PolicyInvalid, PolicyMissing, validate_policy

ROOT = Path(__file__).resolve().parents[2]


def test_checked_in_capability_map_is_valid() -> None:
    capability = load_capability(
        ROOT / ".exo/harness_capability.toml",
        policy_path=ROOT / ".exo/harness_policy.toml",
    )

    assert capability["codex/gpt-luna"] is Difficulty.STANDARD
    assert capability["claude/sonnet"] is Difficulty.HARD


def test_is_capable_respects_the_maximum_rating() -> None:
    capability = CapabilityMap({"codex/gpt-luna": Difficulty.STANDARD})

    assert is_capable("codex/gpt-luna", Difficulty.TRIVIAL, capability)
    assert is_capable("codex/gpt-luna", Difficulty.STANDARD, capability)
    assert not is_capable("codex/gpt-luna", Difficulty.HARD, capability)
    assert not is_capable("missing/model", Difficulty.TRIVIAL, capability)


def test_missing_policy_coverage_fails_closed(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(_policy_toml("missing/model"), encoding="utf-8")
    capability_path = tmp_path / "capability.toml"
    capability_path.write_text(_capability_toml(), encoding="utf-8")

    with pytest.raises(PolicyInvalid, match="missing/model"):
        load_capability(capability_path, policy_path=policy_path)


def test_missing_capability_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PolicyMissing, match="capability file is missing"):
        load_capability(tmp_path / "missing.toml", policy_path=ROOT / ".exo/harness_policy.toml")


def test_unknown_capability_key_is_rejected() -> None:
    document = {"capabilities": {"codex/gpt-luna": "standard"}, "unexpected": True}
    policy = validate_policy(_policy_document())

    with pytest.raises(PolicyInvalid, match="unexpected"):
        validate_capability(document, policy)


def test_unknown_difficulty_is_rejected() -> None:
    policy = validate_policy(_policy_document())

    with pytest.raises(PolicyInvalid, match="unknown difficulty"):
        validate_capability({"capabilities": {"codex/gpt-luna": "expert"}}, policy)


def _policy_toml(harness: str) -> str:
    return f"""[roles.tl]
allow = ["{harness}"]
cost_rank = {{ "{harness}" = 1 }}
token_budget = 1
escalate_after_attempts = 1

[roles.worker]
allow = ["{harness}"]
cost_rank = {{ "{harness}" = 1 }}
token_budget = 1
escalate_after_attempts = 1

[roles.reviewer]
allow = ["{harness}"]
cost_rank = {{ "{harness}" = 1 }}
token_budget = 1
escalate_after_attempts = 1
"""


def _capability_toml() -> str:
    return '[capabilities]\n"codex/gpt-luna" = "standard"\n'


def _policy_document() -> dict[str, object]:
    role = {
        "allow": ["codex/gpt-luna"],
        "cost_rank": {"codex/gpt-luna": 1},
        "token_budget": 1,
        "escalate_after_attempts": 1,
    }
    return {"roles": {"tl": dict(role), "worker": dict(role), "reviewer": dict(role)}}
