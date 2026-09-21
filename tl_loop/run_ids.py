"""Shared naming rules for TL run and recursive sub-TL directory identities."""

from __future__ import annotations

ARCHIVED_ROOT_PREFIX = "root.invalid-"


def uses_reserved_archive_prefix(name: object) -> bool:
    """Return True when ``name`` names a recreate archive, not an active run."""
    return isinstance(name, str) and name.startswith(ARCHIVED_ROOT_PREFIX)
