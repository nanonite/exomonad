"""Regression coverage for #1062: resolve repository identity at continuation.

A continued run whose checkpoint has ``repository_identity: null`` could never
adopt a merged PR: ``_repository_identity`` fails closed, the failure is caught
by ``_reconcile_merged_slice`` and turned into a ``tl-post-merge-<slice>``
gate, and the only writer of ``repository_identity`` was guarded on the caller
already supplying one -- a closed loop with no self-heal. This module reuses
test_legacy_manifest_convergence.py's sanitized "captured Beast" fixtures (see
that module's docstring for why they are not the real captured checkpoint),
but seeds the checkpoint *without* repository_identity -- the exact #1062
shape -- and drives it through the real ``run_tl_loop()`` outer loop.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import JsonObject
from tl_loop.fsm.post_merge import PostMergePhase
from tl_loop.fsm.scope import TLDone as RecursiveTLDone
from tl_loop.loop.driver import (
    REPOSITORY_IDENTITY_GATE_NAME,
    LoopCancelled,
    TLLoopConfig,
    TLLoopError,
    run_tl_loop,
)
from tl_loop.loop.journal import EffectJournal
from tl_loop.state.legacy_manifest import LegacyManifestDisposition, reconcile_legacy_manifest
from tl_loop.state.schema import GateStatus, RepositoryIdentity, SliceStatus
from tl_loop.state.store import RunStore, create
from tl_loop.tests.test_driver import IntegrationTransport, SyntheticQueue
from tl_loop.tests.test_legacy_manifest_convergence import (
    CHAINLINK_ISSUE_ID,
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
    """Same captured-Beast fixture as _seed_and_migrate, but the checkpoint's
    repository_identity is left null -- the exact #1062 shape."""
    create(RUN_ID, {}, root_dir=tmp_path)
    store = RunStore(RUN_ID, tmp_path)
    merge_intent_id = _seed_action_journal(store)
    legacy = _legacy_manifest()
    state = store.load()
    assert state.repository_identity is None
    state = store.checkpoint(
        state.fsm,
        {SLICE_ID: _active_legacy_slice(merge_intent_id)},
        state.budgets,
        state.events.last_consumed_offset,
        plan_manifest=legacy,
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


def _resolved_identity_payload() -> JsonObject:
    return {
        "owner": "org",
        "repo": "repo",
        "base_branch": "main",
        "forge_host": "forge.example.com",
        "remote_url": "https://forge.example.com/org/repo.git",
        "remote_name": "origin",
    }


def _transport_with_identity(resolved: bool) -> RepositoryIdentityTransport:
    base = _merged_watcher_transport()
    return RepositoryIdentityTransport(
        snapshots=base.snapshots,
        identity_response=_resolved_identity_payload() if resolved else None,
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


def test_continuation_resolves_null_identity_and_adopts_merged_pr(tmp_path: Path) -> None:
    _seed_without_repository_identity(tmp_path)
    config = replace(_config(), repository_identity=None)
    transport = _transport_with_identity(resolved=True)
    effects = EffectClient(transport)

    try:
        result = _run_bounded(config, effects, tmp_path)
    except LoopCancelled:
        raise AssertionError(
            "run_tl_loop did not resolve repository identity and converge to "
            "TLDone -- #1062 regressed"
        ) from None

    final = result.final_state
    assert final.repository_identity == RepositoryIdentity(
        owner="org",
        repo="repo",
        base_branch="main",
        forge_host="forge.example.com",
        remote_url="https://forge.example.com/org/repo.git",
    )
    assert isinstance(final.recursive_fsm, RecursiveTLDone)
    assert final.slices[SLICE_ID].status is SliceStatus.MERGED
    assert final.slices[SLICE_ID].post_merge is not None
    assert final.slices[SLICE_ID].post_merge.phase is PostMergePhase.COMPLETE
    assert not final.gates
    # Exactly one identity resolution call, never routed through the watcher
    # for identity purposes.
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


def test_conflicting_supplied_identity_still_raises(tmp_path: Path) -> None:
    store, _migrated = _seed_without_repository_identity(tmp_path)
    store.set_repository_identity(RepositoryIdentity("org", "repo", "main"))
    config = replace(
        _config(),
        repository_identity=RepositoryIdentity("a-different-org", "repo", "main"),
    )
    transport = _transport_with_identity(resolved=True)
    effects = EffectClient(transport)

    with pytest.raises(TLLoopError, match="continuation repository identity differs"):
        _run_bounded(config, effects, tmp_path)


def test_previously_blocked_slice_recovers_once_identity_becomes_available(
    tmp_path: Path,
) -> None:
    """A slice parked by a failed resolution must resume through the same
    post-merge entry point once identity becomes available -- no new spawn,
    branch, or duplicate merge."""
    _seed_without_repository_identity(tmp_path)
    config = replace(_config(), repository_identity=None)

    blocked_transport = _transport_with_identity(resolved=False)
    parked = _run_bounded(config, EffectClient(blocked_transport), tmp_path)
    assert parked.final_state.repository_identity is None
    gate_names = {gate.name: gate.status for gate in parked.final_state.gates}
    assert gate_names.get(REPOSITORY_IDENTITY_GATE_NAME) is GateStatus.PENDING
    assert parked.final_state.slices[SLICE_ID].status is not SliceStatus.MERGED

    recovering_transport = _transport_with_identity(resolved=True)
    try:
        recovered = _run_bounded(config, EffectClient(recovering_transport), tmp_path)
    except LoopCancelled:
        raise AssertionError(
            "a previously-blocked slice did not recover once repository "
            "identity became available -- #1062 regressed"
        ) from None

    final = recovered.final_state
    assert final.repository_identity is not None
    assert final.slices[SLICE_ID].status is SliceStatus.MERGED
    assert final.slices[SLICE_ID].post_merge is not None
    assert final.slices[SLICE_ID].post_merge.phase is PostMergePhase.COMPLETE
    assert isinstance(final.recursive_fsm, RecursiveTLDone)
    # Exactly one merge, one Chainlink close, one changelog, one push --
    # recovery reused the existing owner/PR, it never re-dispatched or
    # re-merged.
    assert [name for name, _ in recovering_transport.calls if name == "merge_pr"] == []
    assert (
        len([n for n, _ in recovering_transport.calls if n == "chainlink_issue_close"]) == 1
    )
    assert (
        len([n for n, _ in recovering_transport.calls if n == "post_merge_changelog"]) == 1
    )
    assert len([n for n, _ in recovering_transport.calls if n == "post_merge_push"]) == 1
    close_calls = [
        arguments
        for name, arguments in recovering_transport.calls
        if name == "chainlink_issue_close"
    ]
    assert close_calls[0]["issue_id"] == CHAINLINK_ISSUE_ID
