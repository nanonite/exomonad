"""Regression coverage for #1062: resolve repository identity at continuation.

A continued run whose checkpoint has ``repository_identity: null`` could never
adopt a merged PR: ``_repository_identity`` fails closed, the failure is caught
by ``_reconcile_merged_slice`` and turned into a ``tl-post-merge-<slice>``
gate, and the only writer of ``repository_identity`` was guarded on the caller
already supplying one -- a closed loop with no self-heal. This module reuses
test_legacy_manifest_convergence.py's sanitized fixtures (see that module's
docstring for why no recorded checkpoint is read),
but seeds the checkpoint *without* repository_identity -- the exact #1062
shape -- and drives it through the real ``run_tl_loop()`` outer loop.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

import tl_loop.loop.driver as driver_module
from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import JsonObject
from tl_loop.loop.driver import (
    REPOSITORY_IDENTITY_GATE_NAME,
    TLLoopConfig,
    TLLoopError,
    WorkPlan,
    _resolve_repository_identity_detail,
    _validate_aggregate_repository_identity,
    run_tl_loop,
)
from tl_loop.loop.journal import EffectJournal
from tl_loop.state.legacy_manifest import LegacyManifestDisposition, reconcile_legacy_manifest
from tl_loop.state.plan_manifest import build_plan_manifest
from tl_loop.state.schema import GateStatus, RepositoryIdentity, SliceStatus
from tl_loop.state.store import RunStore, create
from tl_loop.tests.test_driver import IntegrationTransport, SyntheticQueue
from tl_loop.tests.test_legacy_manifest_convergence import (
    RUN_ID,
    SLICE_ID,
    _active_legacy_slice,
    _candidate_manifest,
    _config,
    _legacy_manifest,
    _merged_watcher_transport,
    _seed_action_journal,
)


def _seed_without_repository_identity(tmp_path: Path):
    """Same sanitized fixture as _seed_and_migrate, but the checkpoint's
    repository_identity is left null -- the exact #1062 shape."""
    create(RUN_ID, {}, root_dir=tmp_path)
    store = RunStore(RUN_ID, tmp_path)
    merge_intent_id = _seed_action_journal(store)
    legacy = _legacy_manifest()
    state = store.load()
    assert state.repository_identity is None
    state = store.set_plan_manifest(
        legacy,
        slices={SLICE_ID: _active_legacy_slice(merge_intent_id)},
    )

    candidate = _candidate_manifest()
    journal = EffectJournal(RUN_ID, store.run_dir / "action-journal.json")
    reconciliation = reconcile_legacy_manifest(
        legacy, candidate, state, journal, child_checkpoint_root=store.run_dir
    )
    assert reconciliation.disposition is LegacyManifestDisposition.PROVEN, reconciliation.reason

    proof = reconciliation.proofs[0]
    rebound = replace(
        state.slices[SLICE_ID],
        branch=proof.branch or state.slices[SLICE_ID].branch,
        worktree=proof.worktree or state.slices[SLICE_ID].worktree,
        legacy_manifest_migration=proof.to_document(),
    )
    migrated = store.set_plan_manifest(candidate, slices={SLICE_ID: rebound})
    return store, migrated


@dataclass
class RepositoryIdentityTransport(IntegrationTransport):
    """Extends the merged-PR watcher transport with a repository_identity effect."""

    identity_response: JsonObject | None = None
    identity_calls: list[JsonObject] = field(default_factory=list)

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        if tool_name == "repository_identity":
            self.calls.append((tool_name, arguments))
            self.identity_calls.append(dict(arguments))
            if self.identity_response is None:
                return {"success": False, "error": "no git remotes configured"}
            return {"success": True, "result": self.identity_response}
        return super().call_tool(role, name, tool_name, arguments)


def _resolved_identity_payload(
    remote_url: str = "https://forge.example.com/org/repo.git",
) -> JsonObject:
    return {"remote_url": remote_url}


def _transport_with_identity(
    resolved: bool,
    remote_url: str = "https://forge.example.com/org/repo.git",
) -> RepositoryIdentityTransport:
    base = _merged_watcher_transport()
    return RepositoryIdentityTransport(
        snapshots=base.snapshots,
        identity_response=_resolved_identity_payload(remote_url) if resolved else None,
    )


def _run_bounded(config: TLLoopConfig, effects: EffectClient, tmp_path: Path):
    """Run run_tl_loop with a hard wall-clock cancellation safety net."""
    cancel_event = threading.Event()
    timer = threading.Timer(10.0, cancel_event.set)
    timer.start()
    try:
        return run_tl_loop(
            RUN_ID,
            None,
            SyntheticQueue([]),
            effects,
            config=replace(
                config,
                cancel_event=cancel_event,
                keep_alive_on_waiting=False,
                poll_interval=0.01,
            ),
            root_dir=tmp_path,
        )
    finally:
        timer.cancel()


def test_continuation_without_child_identity_proof_stays_parked(tmp_path: Path) -> None:
    _seed_without_repository_identity(tmp_path)
    config = replace(_config(), repository_identity=None)
    transport = _transport_with_identity(resolved=True)
    effects = EffectClient(transport)

    result = _run_bounded(config, effects, tmp_path)

    final = result.final_state
    assert final.repository_identity is None
    assert final.repository_identity_migration is None
    assert final.slices[SLICE_ID].status is SliceStatus.IN_REVIEW
    assert final.slices[SLICE_ID].post_merge is None
    assert any(gate.name == REPOSITORY_IDENTITY_GATE_NAME for gate in final.gates)
    assert result.diagnostics["repository_identity_blocker"] == "missing"
    assert len(transport.identity_calls) == 1


def test_failed_resolution_opens_named_gate_without_raising_or_guessing(tmp_path: Path) -> None:
    _seed_without_repository_identity(tmp_path)
    config = replace(_config(), repository_identity=None)
    transport = _transport_with_identity(resolved=False)
    effects = EffectClient(transport)

    result = _run_bounded(config, effects, tmp_path)

    final = result.final_state
    assert final.repository_identity is None
    gate_names = {gate.name: gate.status for gate in final.gates}
    assert gate_names.get(REPOSITORY_IDENTITY_GATE_NAME) is GateStatus.PENDING
    # The slice must not have been silently adopted against a guessed identity.
    assert final.slices[SLICE_ID].status is not SliceStatus.MERGED


def test_beast_root_missing_identity_adopts_matching_legacy_child_repository(
    tmp_path: Path,
) -> None:
    """Reproduce the historical root/child identity shape in temporary state."""
    child_run_id = "substitution-model-architecture-stage"
    remote_url = "http://localhost:3000/exomonad/beast-rs.git"
    legacy_child_identity = {
        "owner": "exomonad",
        "repo": "beast-rs",
        "base_branch": "main",
        "forge_host": "localhost",
        "remote_url": remote_url,
    }
    plan = {
        "sub_tls": [
            {
                "name": child_run_id,
                "task": "integrate architecture changes",
                "order": 1,
                "plan": {"leaves": []},
            }
        ]
    }
    root_manifest = build_plan_manifest(plan, scope_id=RUN_ID, owned_branch="main")
    create(RUN_ID, {"plan_manifest": root_manifest.to_document()}, root_dir=tmp_path)
    root_store = RunStore(RUN_ID, tmp_path)
    child_manifest = root_manifest.child_manifests[f"{RUN_ID}/sub_tl/{child_run_id}"]
    child_state = create(
        child_run_id,
        {
            "parent_run_id": RUN_ID,
            "owner_branch": "main.substitution-model-architecture-stage",
            "plan_manifest": child_manifest.to_document(),
            "repository_identity": legacy_child_identity,
        },
        root_dir=root_store.run_dir,
    )
    assert root_store.load().repository_identity is None
    assert child_state.repository_identity == RepositoryIdentity(remote_url)

    transport = _transport_with_identity(resolved=True, remote_url=remote_url)
    effects = EffectClient(transport)
    _validate_aggregate_repository_identity(
        child_run_id,
        replace(_config(), repository_identity=None, branch="main"),
        effects,
        [],
        root_store,
        publication_attempt=1,
    )

    assert root_store.load().repository_identity == RepositoryIdentity(remote_url)
    assert root_store.load().repository_identity_migration is not None
    assert transport.identity_calls == [{}]


def test_conflicting_supplied_identity_still_raises(tmp_path: Path) -> None:
    store, _migrated = _seed_without_repository_identity(tmp_path)
    store.set_repository_identity(RepositoryIdentity("https://forge.example.com/org/repo.git"))
    config = replace(
        _config(),
        repository_identity=RepositoryIdentity(
            "https://forge.example.com/a-different-org/repo.git"
        ),
    )
    transport = _transport_with_identity(resolved=True)
    effects = EffectClient(transport)

    with pytest.raises(TLLoopError, match="repository_identity_conflict"):
        _run_bounded(config, effects, tmp_path)


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (None, "unavailable"),
        ({}, "missing"),
        ({"remote_url": 17}, "malformed"),
        ({"remote_url": "https://forge.example.com/org/repo.git?route=other"}, "ambiguous"),
    ],
)
def test_identity_authority_failures_have_distinct_codes(
    payload: JsonObject | None,
    expected_code: str,
) -> None:
    transport = _transport_with_identity(resolved=True)
    transport.identity_response = payload

    identity, failure_code = _resolve_repository_identity_detail(
        _config(), EffectClient(transport), []
    )

    assert identity is None
    assert failure_code == expected_code


def test_unavailable_identity_does_not_create_a_new_run(tmp_path: Path) -> None:
    run_id = "missing-identity-new-run"
    store = RunStore(run_id, tmp_path)
    transport = _transport_with_identity(resolved=False)

    with pytest.raises(TLLoopError, match="repository_identity_unavailable"):
        run_tl_loop(
            run_id,
            WorkPlan(),
            SyntheticQueue([]),
            EffectClient(transport),
            config=replace(_config(), run_id=run_id, repository_identity=None),
            root_dir=tmp_path,
        )

    assert not store.path.exists()


def test_new_run_create_includes_resolved_identity_atomically(tmp_path: Path, monkeypatch) -> None:
    run_id = "identity-before-create"
    actual_create = driver_module.create
    observed: dict[str, object] = {}

    def create_with_identity(run_id, root_spec, *, root_dir):
        observed.update(root_spec)
        assert root_spec["repository_identity"] == {
            "remote_url": "https://forge.example.com/org/repo.git"
        }
        return actual_create(run_id, root_spec, root_dir=root_dir)

    monkeypatch.setattr(driver_module, "create", create_with_identity)
    transport = _transport_with_identity(resolved=True)

    run_tl_loop(
        run_id,
        WorkPlan(),
        SyntheticQueue([]),
        EffectClient(transport),
        config=replace(
            _config(),
            run_id=run_id,
            repository_identity=None,
            ledger_run_id=None,
            keep_alive_on_waiting=False,
        ),
        root_dir=tmp_path,
    )

    state = RunStore(run_id, tmp_path).load()
    assert state.repository_identity == RepositoryIdentity("https://forge.example.com/org/repo.git")
    assert observed["repository_identity"] == state.repository_identity.to_document()


def test_missing_identity_without_child_proof_stays_blocked_after_lookup_recovers(
    tmp_path: Path,
) -> None:
    """A current remote cannot establish historical ownership without child proof."""
    _seed_without_repository_identity(tmp_path)
    config = replace(_config(), repository_identity=None)

    blocked_transport = _transport_with_identity(resolved=False)
    parked = _run_bounded(config, EffectClient(blocked_transport), tmp_path)
    assert parked.final_state.repository_identity is None
    gate_names = {gate.name: gate.status for gate in parked.final_state.gates}
    assert gate_names.get(REPOSITORY_IDENTITY_GATE_NAME) is GateStatus.PENDING
    assert parked.final_state.slices[SLICE_ID].status is not SliceStatus.MERGED

    recovering_transport = _transport_with_identity(resolved=True)
    recovered = _run_bounded(config, EffectClient(recovering_transport), tmp_path)
    final = recovered.final_state
    assert final.repository_identity is None
    assert final.slices[SLICE_ID].status is SliceStatus.IN_REVIEW
    assert final.slices[SLICE_ID].post_merge is None
    assert recovered.diagnostics["repository_identity_blocker"] == "missing"
    assert any(gate.name == REPOSITORY_IDENTITY_GATE_NAME for gate in final.gates)
    assert [name for name, _ in recovering_transport.calls if name == "merge_pr"] == []
    assert not any(
        name in {"chainlink_issue_close", "post_merge_changelog", "post_merge_push"}
        for name, _ in recovering_transport.calls
    )
    assert not any(name in {"spawn_leaf", "spawn_worker"} for name, _ in recovering_transport.calls)
