"""Durable migration of missing and legacy repository identity checkpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tl_loop.state.store as store_module
from tl_loop.state.lock import RunLock
from tl_loop.state.plan_manifest import build_plan_manifest
from tl_loop.state.repository_identity_migration import RepositoryIdentityMigrationError
from tl_loop.state.schema import RepositoryIdentity
from tl_loop.state.store import RunStore, create

CANONICAL_URL = "https://forge.example.com/Org/Repo.git"
LEGACY_CHILD_IDENTITY = {
    "owner": "Org",
    "repo": "Repo",
    "base_branch": "main",
    "forge_host": "forge.example.com",
    "remote_url": CANONICAL_URL,
}


def _seed_root_and_child(
    root_dir: Path,
    *,
    root_identity: object = None,
    child_identity: object = LEGACY_CHILD_IDENTITY,
    include_root_identity: bool = False,
) -> tuple[RunStore, RunStore]:
    plan = {
        "sub_tls": [
            {
                "name": "stage",
                "task": "integrate stage work",
                "order": 1,
                "plan": {},
            }
        ]
    }
    manifest = build_plan_manifest(plan, scope_id="root", owned_branch="main")
    root_spec: dict[str, object] = {"plan_manifest": manifest.to_document()}
    if include_root_identity:
        root_spec["repository_identity"] = root_identity
    create("root", root_spec, root_dir=root_dir)
    root = RunStore("root", root_dir)

    child_manifest = manifest.child_manifests["root/sub_tl/stage"]
    child_spec: dict[str, object] = {
        "parent_run_id": "root",
        "owner_branch": "main.stage",
        "plan_manifest": child_manifest.to_document(),
    }
    if child_identity is not None:
        child_spec["repository_identity"] = child_identity
    create("stage", child_spec, root_dir=root.run_dir)
    return root, RunStore("stage", root.run_dir)


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_missing_root_identity_migrates_only_with_matching_declared_child(tmp_path: Path) -> None:
    root, child = _seed_root_and_child(tmp_path)
    before = _read(root.path)
    child_before = _read(child.path)
    stage_slice = before["slices"]["stage"]
    stage_slice["publication"] = {
        "pr_number": 46,
        "head_sha": "571c83a324de31b0c4dc5f60e37eb476e7c5af05",
        "head_branch": "main.stage",
        "base_branch": "main",
        "attempt": 1,
        "invocation_id": "historical-invocation",
    }
    stage_slice["pr_number"] = 46
    root.path.write_text(json.dumps(before), encoding="utf-8")
    before = _read(root.path)
    receipt_path = root.run_dir / "accepted-publication-receipt.json"
    receipt_path.write_text(
        '{"pr_number":46,"head_sha":"571c83a","receipt":"retained"}\n',
        encoding="utf-8",
    )
    receipt_before = receipt_path.read_bytes()

    migrated = root.migrate_repository_identity(
        RepositoryIdentity(CANONICAL_URL), corroborating_sub_tls=("stage",)
    )

    after = _read(root.path)
    assert migrated.repository_identity == RepositoryIdentity(CANONICAL_URL)
    assert after["repository_identity"] == {"remote_url": CANONICAL_URL}
    assert after["slices"] == before["slices"]
    assert after["slices"]["stage"]["publication"] == before["slices"]["stage"]["publication"]
    assert after["revision"] == before["revision"] + 1
    audit = after["repository_identity_migration"]
    assert audit["source_shape"] == "missing"
    assert audit["reason_code"] == "bootstrap_missing_identity"
    assert audit["source_checkpoint_sha256"]
    assert audit["corroborating_children"][0]["run_id"] == "stage"
    assert _read(child.path) == child_before
    assert receipt_path.read_bytes() == receipt_before

    journal = _read(root.run_dir / "repository-identity-migration.json")
    assert journal["status"] == "complete"
    assert "forge.example.com" not in json.dumps(journal)
    root.set_repository_identity(RepositoryIdentity(CANONICAL_URL))
    revision_after_followup_write = _read(root.path)["revision"]

    replayed = root.migrate_repository_identity(RepositoryIdentity(CANONICAL_URL))

    assert replayed.repository_identity == RepositoryIdentity(CANONICAL_URL)
    assert _read(root.path)["revision"] == revision_after_followup_write
    assert _read(root.run_dir / "repository-identity-migration.json")["audit"] == audit


def test_missing_root_identity_without_child_proof_fails_closed(tmp_path: Path) -> None:
    create("root", {}, root_dir=tmp_path)
    root = RunStore("root", tmp_path)
    before = root.path.read_bytes()

    with pytest.raises(RepositoryIdentityMigrationError) as rejected:
        root.migrate_repository_identity(RepositoryIdentity(CANONICAL_URL))

    assert rejected.value.code == "missing"
    assert root.path.read_bytes() == before
    assert not (root.run_dir / "repository-identity-migration.json").exists()


def test_legacy_identity_is_canonicalized_after_authoritative_comparison(tmp_path: Path) -> None:
    legacy = {
        **LEGACY_CHILD_IDENTITY,
        "owner": "historical-owner-metadata",
        "repo": "historical-repo-metadata",
        "base_branch": "historical-target-metadata",
        "forge_host": "historical-forge-metadata",
        "remote_url": "HTTPS://worker:secret@FORGE.example.com/Org/Repo.git",
    }
    root, _child = _seed_root_and_child(
        tmp_path,
        root_identity=legacy,
        include_root_identity=True,
    )

    state = root.migrate_repository_identity(RepositoryIdentity(CANONICAL_URL))

    assert state.repository_identity == RepositoryIdentity(CANONICAL_URL)
    persisted = _read(root.path)
    assert persisted["repository_identity"] == {"remote_url": CANONICAL_URL}
    assert persisted["repository_identity_migration"]["source_shape"] == "legacy"
    assert "secret" not in root.path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("root_identity", "child_identity", "observed_url", "expected_code"),
    [
        (None, None, CANONICAL_URL, "missing"),
        (
            None,
            {**LEGACY_CHILD_IDENTITY, "remote_url": "https://forge.example.com/other/repo.git"},
            CANONICAL_URL,
            "conflicting",
        ),
        (
            {**LEGACY_CHILD_IDENTITY, "remote_url": "https://forge.example.com/other/repo.git"},
            LEGACY_CHILD_IDENTITY,
            CANONICAL_URL,
            "conflicting",
        ),
        (
            {**LEGACY_CHILD_IDENTITY, "remote_url": "https://forge.example.com/org/repo%ZZ.git"},
            LEGACY_CHILD_IDENTITY,
            CANONICAL_URL,
            "malformed",
        ),
        ({"remote_url": ""}, LEGACY_CHILD_IDENTITY, CANONICAL_URL, "malformed"),
        (
            {
                **LEGACY_CHILD_IDENTITY,
                "remote_url": "https://forge.example.com/org/repo.git?repo=other",
            },
            LEGACY_CHILD_IDENTITY,
            CANONICAL_URL,
            "ambiguous",
        ),
    ],
)
def test_identity_failures_are_classified_without_mutating_checkpoint(
    tmp_path: Path,
    root_identity: object,
    child_identity: object,
    observed_url: str,
    expected_code: str,
) -> None:
    invalid_shape = expected_code in {"malformed", "ambiguous"}
    root, _child = _seed_root_and_child(
        tmp_path,
        root_identity=None if invalid_shape else root_identity,
        child_identity=child_identity,
        include_root_identity=root_identity is not None and not invalid_shape,
    )
    if invalid_shape:
        document = _read(root.path)
        document["repository_identity"] = root_identity
        root.path.write_text(json.dumps(document), encoding="utf-8")
    before = root.path.read_bytes()

    with pytest.raises(RepositoryIdentityMigrationError) as rejected:
        root.migrate_repository_identity(
            RepositoryIdentity(observed_url), corroborating_sub_tls=("stage",)
        )

    assert rejected.value.code == expected_code
    assert root.path.read_bytes() == before
    assert not (root.run_dir / "repository-identity-migration.json").exists()


def test_stale_checkpoint_fingerprint_refuses_apply(tmp_path: Path, monkeypatch) -> None:
    root, _child = _seed_root_and_child(tmp_path)
    real_apply = store_module.apply

    def race_before_apply(run_dir, mutator, **kwargs):
        if Path(run_dir) == root.run_dir:
            real_apply(run_dir, lambda document: document)
        return real_apply(run_dir, mutator, **kwargs)

    monkeypatch.setattr(store_module, "apply", race_before_apply)
    with pytest.raises(RepositoryIdentityMigrationError) as rejected:
        root.migrate_repository_identity(
            RepositoryIdentity(CANONICAL_URL), corroborating_sub_tls=("stage",)
        )

    assert rejected.value.code == "stale"
    assert "repository_identity" not in _read(root.path)
    assert _read(root.run_dir / "repository-identity-migration.json")["status"] == "prepared"


def test_missing_child_evidence_is_unavailable_without_checkpoint_mutation(tmp_path: Path) -> None:
    root, child = _seed_root_and_child(tmp_path)
    before = root.path.read_bytes()
    child.path.unlink()

    with pytest.raises(RepositoryIdentityMigrationError) as rejected:
        root.migrate_repository_identity(
            RepositoryIdentity(CANONICAL_URL), corroborating_sub_tls=("stage",)
        )

    assert rejected.value.code == "unavailable"
    assert root.path.read_bytes() == before
    assert not (root.run_dir / "repository-identity-migration.json").exists()


def test_lock_timeout_is_retryable_and_does_not_prepare_transition(tmp_path: Path) -> None:
    root, _child = _seed_root_and_child(tmp_path)

    with (
        RunLock(root.run_dir / "repository-identity.lock"),
        pytest.raises(RepositoryIdentityMigrationError) as rejected,
    ):
        root.migrate_repository_identity(
            RepositoryIdentity(CANONICAL_URL),
            corroborating_sub_tls=("stage",),
            lock_timeout=0.01,
        )

    assert rejected.value.code == "unavailable"
    assert "repository_identity" not in _read(root.path)
    assert not (root.run_dir / "repository-identity-migration.json").exists()


def test_malformed_transition_journal_fails_closed(tmp_path: Path) -> None:
    root, _child = _seed_root_and_child(tmp_path)
    journal_path = root.run_dir / "repository-identity-migration.json"
    journal_path.write_text("{}", encoding="utf-8")
    before = root.path.read_bytes()

    with pytest.raises(RepositoryIdentityMigrationError) as rejected:
        root.migrate_repository_identity(
            RepositoryIdentity(CANONICAL_URL), corroborating_sub_tls=("stage",)
        )

    assert rejected.value.code == "malformed"
    assert root.path.read_bytes() == before


def test_interrupted_migration_rejects_changed_child_fingerprint(
    tmp_path: Path, monkeypatch
) -> None:
    root, child = _seed_root_and_child(tmp_path)
    real_apply = store_module.apply

    def interrupt_root(run_dir, mutator, **kwargs):
        if Path(run_dir) == root.run_dir:
            raise RuntimeError("simulated interruption before identity checkpoint write")
        return real_apply(run_dir, mutator, **kwargs)

    monkeypatch.setattr(store_module, "apply", interrupt_root)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        root.migrate_repository_identity(
            RepositoryIdentity(CANONICAL_URL), corroborating_sub_tls=("stage",)
        )
    monkeypatch.setattr(store_module, "apply", real_apply)
    real_apply(child.run_dir, lambda document: document)
    before = root.path.read_bytes()

    with pytest.raises(RepositoryIdentityMigrationError) as rejected:
        root.migrate_repository_identity(
            RepositoryIdentity(CANONICAL_URL), corroborating_sub_tls=("stage",)
        )

    assert rejected.value.code == "stale"
    assert root.path.read_bytes() == before


@pytest.mark.parametrize("crash_after_apply", [False, True])
def test_interrupted_migration_replays_once(
    tmp_path: Path, monkeypatch, crash_after_apply: bool
) -> None:
    root, _child = _seed_root_and_child(tmp_path)
    real_apply = store_module.apply

    def interrupt(run_dir, mutator, **kwargs):
        if Path(run_dir) != root.run_dir:
            return real_apply(run_dir, mutator, **kwargs)
        if not crash_after_apply:
            raise RuntimeError("simulated interruption before identity checkpoint write")
        real_apply(run_dir, mutator, **kwargs)
        raise RuntimeError("simulated interruption after identity checkpoint write")

    monkeypatch.setattr(store_module, "apply", interrupt)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        root.migrate_repository_identity(
            RepositoryIdentity(CANONICAL_URL), corroborating_sub_tls=("stage",)
        )
    revision_at_crash = _read(root.path)["revision"]
    transition_before = _read(root.run_dir / "repository-identity-migration.json")["audit"][
        "transition_id"
    ]

    monkeypatch.setattr(store_module, "apply", real_apply)
    resumed = root.migrate_repository_identity(
        RepositoryIdentity(CANONICAL_URL), corroborating_sub_tls=("stage",)
    )

    final = _read(root.path)
    journal = _read(root.run_dir / "repository-identity-migration.json")
    assert resumed.repository_identity == RepositoryIdentity(CANONICAL_URL)
    assert journal["status"] == "complete"
    assert journal["audit"]["transition_id"] == transition_before
    assert final["revision"] == revision_at_crash + (0 if crash_after_apply else 1)
