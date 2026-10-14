"""Test checkpoint factories with explicit current and historical identities."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from tl_loop.state.schema import LEGACY_IDENTITY_CHECKPOINT_VERSION
from tl_loop.state.store import RunState, RunStore, create as create_checkpoint

TEST_REPOSITORY_IDENTITY: dict[str, str] = {
    "remote_url": "https://forge.example.test/test/repository.git",
}


def create(
    run_id: str,
    root_spec: Mapping[str, object],
    *,
    root_dir: str | Path,
) -> RunState:
    """Create a normal current test run with a declared canonical identity."""
    current_spec = dict(root_spec)
    current_spec.setdefault("repository_identity", dict(TEST_REPOSITORY_IDENTITY))
    return create_checkpoint(run_id, current_spec, root_dir=root_dir)


def create_legacy_identity_checkpoint(
    run_id: str,
    root_spec: Mapping[str, object],
    *,
    root_dir: str | Path,
    repository_identity: object = None,
) -> None:
    """Seed explicit version-4 state for repository identity migration tests."""
    current_spec = dict(root_spec)
    current_spec["repository_identity"] = dict(TEST_REPOSITORY_IDENTITY)
    create_checkpoint(run_id, current_spec, root_dir=root_dir)

    mark_legacy_identity_checkpoint(
        RunStore(run_id, Path(root_dir)), repository_identity=repository_identity
    )


def mark_legacy_identity_checkpoint(
    store: RunStore,
    *,
    repository_identity: object = None,
) -> None:
    """Downgrade an already-prepared fixture to explicit version-4 identity state."""
    checkpoint_path = store.path
    document = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    document["version"] = LEGACY_IDENTITY_CHECKPOINT_VERSION
    if repository_identity is None:
        document.pop("repository_identity", None)
    else:
        document["repository_identity"] = dict(repository_identity)
    checkpoint_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
