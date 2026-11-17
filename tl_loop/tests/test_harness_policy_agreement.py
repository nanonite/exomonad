"""Pin the Rust and Python definitions of a configured harness to one fixture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from tl_loop.loop.escalate import authorize_harness_switch
from tl_loop.select.agent_type import policy_approved_candidates
from tl_loop.select.harness import canonical_agent_type, parse_harness_identifier
from tl_loop.select.policy import load_policy

FIXTURE_ROOT = (
    Path(__file__).resolve().parents[2] / "rust" / "exomonad-core" / "tests" / "fixtures"
)
POLICY_PATH = FIXTURE_ROOT / "harness_policy_agreement.toml"
EXPECTED_PATH = FIXTURE_ROOT / "harness_policy_agreement.json"
ROLES = ("tl", "worker", "reviewer")


def _fixture() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(EXPECTED_PATH.read_text(encoding="utf-8")),
    )


def _expected() -> dict[str, list[str]]:
    return cast(dict[str, list[str]], _fixture()["canonical"])


def test_python_policy_loader_matches_the_shared_allowlist_fixture() -> None:
    policy = load_policy(POLICY_PATH)
    expected = _expected()

    for role in ROLES:
        canonical = {
            canonical_agent_type(identifier) for identifier in policy.roles[role].allow
        }
        assert canonical == set(expected[role]), (
            f"Python role {role!r} canonical allowlist disagrees with the shared fixture"
        )


def test_python_policy_entries_match_the_shared_fixture() -> None:
    policy = load_policy(POLICY_PATH)
    entries = cast(dict[str, list[dict[str, object]]], _fixture()["entries"])

    for role in ROLES:
        parsed = [
            parse_harness_identifier(identifier) for identifier in policy.roles[role].allow
        ]
        expected = [
            (entry["harness"], entry["model"]) for entry in entries[role]
        ]
        assert [(route.agent_type, route.model) for route in parsed] == expected, (
            f"Python role {role!r} entries disagree with the shared fixture"
        )


def test_python_grammar_matches_the_shared_fixture() -> None:
    fixture = _fixture()
    for row in cast(list[dict[str, object]], fixture["grammar"]):
        route = parse_harness_identifier(cast(str, row["identifier"]))
        assert route.agent_type == row["harness"], row["identifier"]
        assert route.model == row["model"], row["identifier"]
    for identifier in cast(list[str], fixture["invalid_grammar"]):
        with pytest.raises(ValueError):
            parse_harness_identifier(identifier)


def test_python_route_cases_match_the_shared_fixture() -> None:
    policy = load_policy(POLICY_PATH)
    cases = cast(list[dict[str, object]], _fixture()["route_cases"])

    for case in cases:
        role = cast(str, case["role"])
        harness = cast(str, case["harness"])
        model = cast(str | None, case["model"])
        request = harness if model is None else f"{harness}/{model}"
        approved = policy_approved_candidates(request, policy.roles[role])
        assert bool(approved) is case["allowed"], (
            f"Python role {role!r} route {request!r} disagrees with the shared fixture"
        )


@pytest.mark.parametrize("role", ROLES)
def test_python_switch_guard_agrees_with_the_shared_allowlist(role: str) -> None:
    policy = load_policy(POLICY_PATH)
    expected = _expected()
    allow = policy.roles[role].allow

    for harness in ("claude", "codex", "opencode", "shoal"):
        decision = authorize_harness_switch(
            "codex",
            harness,
            "configured route comparison",
            "model-a",
            "high",
            allow,
            env={},
        )
        assert decision.allowed is (harness in expected[role]), (
            f"Python guard accepted {harness!r} for {role!r} against the shared fixture"
        )


def test_qualified_policy_entries_authorize_the_bare_harness_name() -> None:
    decision = authorize_harness_switch(
        "codex",
        "opencode",
        "review repair",
        "deepseek-v4.1-flash",
        "high",
        ["opencode/deepseek-v4.1-flash"],
        env={},
    )

    assert decision.allowed is True
    assert decision.cause is None


def test_unlisted_harness_still_requires_the_operator_flag() -> None:
    without_flag = authorize_harness_switch(
        "codex", "opencode", "review repair", "model-a", "high", ["codex/gpt-5.6-luna"], env={}
    )
    with_flag = authorize_harness_switch(
        "codex",
        "opencode",
        "review repair",
        "model-a",
        "high",
        ["codex/gpt-5.6-luna"],
        env={"EXOMONAD_ALLOW_HARNESS_SWITCH": "1"},
    )

    assert without_flag.allowed is False
    assert without_flag.cause is not None
    assert with_flag.allowed is True
