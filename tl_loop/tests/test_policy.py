"""Fail-closed validation for the human-authored harness policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from tl_loop.select.policy import PolicyInvalid, PolicyMissing, load_policy, validate_policy


def test_checked_in_policy_is_valid() -> None:
    policy = load_policy(Path(".exo/harness_policy.toml"))

    assert policy.roles["tl"].allow == ("codex/gpt-luna",)
    assert policy.roles["reviewer"].allow == ("codex/gpt-luna",)
    assert policy.roles["worker"].allow[0] == "codex/gpt-luna"


def test_missing_policy_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PolicyMissing, match="policy file is missing"):
        load_policy(tmp_path / "missing.toml")


def test_unknown_root_key_names_the_offending_key() -> None:
    document = _valid_document()
    document["unexpected"] = True

    _assert_invalid(document, "unexpected")


def test_unknown_role_and_role_key_are_rejected() -> None:
    document = _valid_document()
    roles = document["roles"]
    assert isinstance(roles, dict)
    roles["operator"] = roles["tl"]
    _assert_invalid(document, "operator")

    document = _valid_document()
    roles = document["roles"]
    assert isinstance(roles, dict)
    tl = roles["tl"]
    assert isinstance(tl, dict)
    tl["unexpected"] = True
    _assert_invalid(document, "unexpected")


def test_every_allowed_harness_requires_a_cost_rank() -> None:
    document = _valid_document()
    roles = document["roles"]
    assert isinstance(roles, dict)
    worker = roles["worker"]
    assert isinstance(worker, dict)
    worker["allow"] = ["codex/gpt-luna", "claude/sonnet"]

    _assert_invalid(document, "claude/sonnet")


def test_cost_rank_for_unallowed_harness_is_rejected() -> None:
    document = _valid_document()
    roles = document["roles"]
    assert isinstance(roles, dict)
    worker = roles["worker"]
    assert isinstance(worker, dict)
    cost_rank = worker["cost_rank"]
    assert isinstance(cost_rank, dict)
    cost_rank["claude/sonnet"] = 2

    _assert_invalid(document, "claude/sonnet")


def test_per_harness_budget_must_name_an_allowed_harness() -> None:
    document = _valid_document()
    roles = document["roles"]
    assert isinstance(roles, dict)
    worker = roles["worker"]
    assert isinstance(worker, dict)
    worker["per_harness_budget"] = {"claude/sonnet": 100}

    _assert_invalid(document, "claude/sonnet")


@pytest.mark.parametrize("field", ["token_budget", "escalate_after_attempts"])
def test_non_positive_scalar_budget_is_rejected(field: str) -> None:
    document = _valid_document()
    roles = document["roles"]
    assert isinstance(roles, dict)
    worker = roles["worker"]
    assert isinstance(worker, dict)
    worker[field] = 0

    _assert_invalid(document, field)


def test_non_positive_per_harness_budget_is_rejected() -> None:
    document = _valid_document()
    roles = document["roles"]
    assert isinstance(roles, dict)
    worker = roles["worker"]
    assert isinstance(worker, dict)
    worker["per_harness_budget"] = {"codex/gpt-luna": 0}

    _assert_invalid(document, "codex/gpt-luna")


def _valid_document() -> dict[str, object]:
    role = {
        "allow": ["codex/gpt-luna"],
        "cost_rank": {"codex/gpt-luna": 1},
        "token_budget": 100,
        "per_harness_budget": {"codex/gpt-luna": 100},
        "escalate_after_attempts": 1,
    }
    return {"roles": {"tl": dict(role), "worker": dict(role), "reviewer": dict(role)}}


def _assert_invalid(document: dict[str, object], expected: str) -> None:
    with pytest.raises(PolicyInvalid, match=expected):
        validate_policy(document)
