"""Coverage for the Rust-catalog model resolution ladder."""

from __future__ import annotations

from pathlib import Path

import pytest

from tl_loop.select.classify import Difficulty
from tl_loop.select.model import (
    ModelCatalog,
    ModelResolutionError,
    load_model_catalog,
    parse_thinking_suffix,
    select_model,
    select_model_for_difficulty,
)

FIXTURE = Path(__file__).parent / "fixtures" / "model_catalog.json"
SCORED_FIXTURE = Path(__file__).parent / "fixtures" / "model_catalog_scored.json"
CATALOG = ModelCatalog.from_fixture(FIXTURE)
SCORED_CATALOG = ModelCatalog.from_fixture(SCORED_FIXTURE)


def test_exact_reference_is_selected_first() -> None:
    choice = select_model("codex", CATALOG, "gpt-5.5")

    assert choice.model_id == "gpt-5.5"
    assert choice.ladder_rung_used == "exact_reference"


def test_alias_is_preferred_over_dated_versions() -> None:
    choice = select_model("claude", CATALOG, "claude-sonnet")

    assert choice.model_id == "claude-sonnet-4-6"
    assert choice.ladder_rung_used == "alias_preferred"


def test_latest_dated_version_is_selected_without_alias() -> None:
    choice = select_model("codex", CATALOG, "gpt-5-codex")

    assert choice.model_id == "gpt-5-codex-20260101"
    assert choice.ladder_rung_used == "latest_dated"


def test_provider_default_is_used_after_pattern_misses() -> None:
    choice = select_model("codex", CATALOG, "missing", provider_default="gpt-5.5")

    assert choice.model_id == "gpt-5.5"
    assert choice.ladder_rung_used == "provider_default"


def test_fallback_uses_first_catalog_model() -> None:
    choice = select_model("codex", CATALOG, None)

    assert choice.model_id == "gpt-5.5"
    assert choice.ladder_rung_used == "fallback"


def test_thinking_suffix_is_separate_from_model_id() -> None:
    base, level = parse_thinking_suffix("gpt-5.5:high")
    choice = select_model("codex", CATALOG, "gpt-5.5:high")

    assert (base, level) == ("gpt-5.5", "high")
    assert choice.model_id == "gpt-5.5"
    assert choice.thinking_level == "high"


def test_invalid_suffix_remains_part_of_reference() -> None:
    assert parse_thinking_suffix("gpt-5.5:turbo") == ("gpt-5.5:turbo", None)


def test_missing_explicit_canonical_reference_raises() -> None:
    with pytest.raises(ModelResolutionError, match="exact model reference"):
        select_model("codex", CATALOG, "codex/missing")


def test_explicit_exact_bare_reference_can_be_strict() -> None:
    with pytest.raises(ModelResolutionError, match="exact model reference"):
        select_model("codex", CATALOG, "missing", exact_reference=True)


def test_harness_catalog_is_constrained() -> None:
    with pytest.raises(ModelResolutionError, match="no models available"):
        select_model("unknown", CATALOG, None)


def test_effect_payload_uses_normalized_records() -> None:
    payload = {"models": [{"harness": "codex", "model_id": "gpt-5.5"}]}
    choice = select_model("codex", payload, "gpt-5.5")

    assert choice.model_id == "gpt-5.5"


def test_standard_difficulty_picks_cheapest_per_intelligence_point() -> None:
    choice = select_model_for_difficulty("codex", SCORED_CATALOG, Difficulty.STANDARD)

    assert choice.model_id == "gpt-5-mini"
    assert choice.ladder_rung_used == "cheapest_capable"


def test_trivial_difficulty_also_picks_cheapest() -> None:
    choice = select_model_for_difficulty("claude", SCORED_CATALOG, Difficulty.TRIVIAL)

    assert choice.model_id == "claude-haiku"
    assert choice.ladder_rung_used == "cheapest_capable"


def test_hard_difficulty_picks_strongest() -> None:
    choice = select_model_for_difficulty("codex", SCORED_CATALOG, Difficulty.HARD)

    assert choice.model_id == "gpt-5.5"
    assert choice.ladder_rung_used == "difficulty_strong"


def test_escalation_picks_strongest_even_for_standard_difficulty() -> None:
    choice = select_model_for_difficulty(
        "claude", SCORED_CATALOG, Difficulty.STANDARD, escalated=True
    )

    assert choice.model_id == "claude-sonnet-4-6"
    assert choice.ladder_rung_used == "escalation_strong"


def test_unscored_records_remain_selectable_but_deprioritized() -> None:
    payload = {
        "models": [
            {"harness": "codex", "model_id": "gpt-5.5", "coding_score": 80.0, "price_per_1m_tokens": 5.0},
            {"harness": "codex", "model_id": "gpt-legacy"},
        ]
    }
    catalog = ModelCatalog.from_payload(payload)

    standard = select_model_for_difficulty("codex", catalog, Difficulty.STANDARD)
    hard = select_model_for_difficulty("codex", catalog, Difficulty.HARD)

    assert standard.model_id == "gpt-5.5"
    assert hard.model_id == "gpt-5.5"


def test_difficulty_selection_fails_closed_for_unknown_harness() -> None:
    with pytest.raises(ModelResolutionError, match="no models available"):
        select_model_for_difficulty("unknown", SCORED_CATALOG, Difficulty.STANDARD)


def test_load_model_catalog_absent_returns_none(tmp_path: Path) -> None:
    assert load_model_catalog(tmp_path / "missing.json") is None


def test_load_model_catalog_invalid_raises(tmp_path: Path) -> None:
    bad = tmp_path / "model-catalog.json"
    bad.write_text("{ not json", encoding="utf-8")

    with pytest.raises(ModelResolutionError):
        load_model_catalog(bad)


@pytest.mark.parametrize(
    "coding_score",
    [-1.0, 101.0, float("nan"), float("inf")],
)
def test_invalid_coding_score_is_rejected(coding_score: float) -> None:
    payload = {
        "models": [
            {"harness": "codex", "model_id": "gpt-5.5", "coding_score": coding_score}
        ]
    }

    with pytest.raises(ModelResolutionError, match="coding_score"):
        ModelCatalog.from_payload(payload)


@pytest.mark.parametrize(
    "price",
    [-0.01, float("nan"), float("inf")],
)
def test_invalid_price_is_rejected(price: float) -> None:
    payload = {
        "models": [
            {
                "harness": "codex",
                "model_id": "gpt-5.5",
                "coding_score": 80.0,
                "price_per_1m_tokens": price,
            }
        ]
    }

    with pytest.raises(ModelResolutionError, match="price_per_1m_tokens"):
        ModelCatalog.from_payload(payload)
