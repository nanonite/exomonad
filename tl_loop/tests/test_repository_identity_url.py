"""Shared URL contract vectors for Python and Rust repository identity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from tl_loop.state.repository_identity import (
    canonical_repository_url,
    repository_api_coordinates,
)

_VECTORS = Path(__file__).parent / "fixtures" / "repository_identity_url_vectors.json"


def _vectors() -> list[dict[str, str]]:
    value = json.loads(_VECTORS.read_text(encoding="utf-8"))
    assert isinstance(value, list)
    return [cast(dict[str, str], vector) for vector in value]


@pytest.mark.parametrize("vector", _vectors(), ids=lambda vector: vector["name"])
def test_canonical_repository_url_shared_vectors(vector: dict[str, str]) -> None:
    if "canonical" in vector:
        canonical = canonical_repository_url(vector["input"])
        assert canonical == vector["canonical"]
        assert canonical_repository_url(canonical) == canonical
        assert "sample-secret" not in canonical
        assert "alice" not in canonical
    else:
        with pytest.raises(ValueError) as rejected:
            canonical_repository_url(vector["input"])
        assert vector["error"] in str(rejected.value)
        assert "sample-secret" not in str(rejected.value)


def test_repository_api_coordinates_are_derived_from_canonical_path() -> None:
    assert repository_api_coordinates("https://forge.example.com/Org/Repo.git") == (
        "Org",
        "Repo",
    )


def test_repository_api_coordinates_reject_insufficient_path() -> None:
    with pytest.raises(ValueError, match="does not contain an API owner"):
        repository_api_coordinates("https://forge.example.com/repo.git")
