"""Prove the harness carries no project-specific content.

The nested-docker failure is a class of failure, not one project's incident.
This acceptance must therefore run a generic scratch repository from a generic
job image, and never borrow another project's workspace content, image, or
repository name. The terms below are assembled from fragments so this checker
does not itself contain the strings it searches for.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

#: Content that belongs to a different project and must not appear here.
FORBIDDEN_TERMS = (
    ("be" "ast"),
    ("ghcr.io/" "catthehacker"),
    ("be" "ast-rs-" "forgejo-ci"),
    ("forgejo-runner-" "221"),
    ("exomonad-" "1059-forgejo"),
)

_TEXT_SUFFIXES = {".py", ".sh", ".md", ".yml", ".yaml", ".toml", ".txt", ".dockerfile"}
_TEXT_NAMES = {"Dockerfile", "justfile"}
_SKIPPED_DIRS = {"__pycache__", ".git", ".venv"}


def _text_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIPPED_DIRS for part in path.parts):
            continue
        if path.suffix in _TEXT_SUFFIXES or path.name in _TEXT_NAMES:
            yield path


def find_project_specific(roots: Iterable[Path]) -> list[str]:
    """Return one finding per forbidden term occurrence under ``roots``."""
    findings: list[str] = []
    for root in roots:
        for path in _text_files(root):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            lowered = text.lower()
            for term in FORBIDDEN_TERMS:
                if term.lower() in lowered:
                    findings.append(f"{path}: contains {term!r}")
    return findings
