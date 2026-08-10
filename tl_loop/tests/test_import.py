"""Smoke coverage for the package boundary."""

import importlib


def test_package_imports() -> None:
    """The root package must be importable before controller logic is added."""
    package = importlib.import_module("tl_loop")
    assert package.__name__ == "tl_loop"
