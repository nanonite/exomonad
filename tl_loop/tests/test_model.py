"""Coverage for the Rust-catalog model resolution ladder."""

from __future__ import annotations

from pathlib import Path

import pytest

from tl_loop.select.model import (
    ModelCatalog,
    ModelResolutionError,
    parse_thinking_suffix,
    select_model,
)

FIXTURE = Path(__file__).parent / "fixtures" / "model_catalog.json"
CATALOG = ModelCatalog.from_fixture(FIXTURE)


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
