"""Regression tests for the observability contract validator."""

from __future__ import annotations

from pathlib import Path

import pytest
from validate_observability_contracts import ContractError, validate_declared_producers


def test_declared_types_require_non_test_source(tmp_path: Path) -> None:
    test_source = tmp_path / "rust/tests/fixture.rs"
    test_source.parent.mkdir(parents=True)
    test_source.write_text("test.only", encoding="utf-8")
    event_types = [
        {"type": "production.event"},
        {"type": "test.only"},
        {"type": "custom"},
    ]

    with pytest.raises(ContractError, match="test.only"):
        validate_declared_producers(tmp_path, event_types)

    production_source = tmp_path / "rust/src/producer.rs"
    production_source.parent.mkdir(parents=True)
    production_source.write_text("production.event test.only", encoding="utf-8")
    validate_declared_producers(tmp_path, event_types)
