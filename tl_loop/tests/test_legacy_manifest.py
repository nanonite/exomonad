"""Regression coverage for evidence-bound active manifest migration."""

from types import SimpleNamespace

from tl_loop.state.legacy_manifest import (
    LegacyManifestDisposition,
    reconcile_legacy_manifest,
)
from tl_loop.state.plan_manifest import build_legacy_manifest, build_plan_manifest
from tl_loop.state.schema import (
    ActionKind,
    ActionPhase,
    DurableReviewEvidence,
    HandoffEvidence,
    PublicationBinding,
    SliceStatus,
    Verdict,
    ActionState,
)


class SnapshotJournal:
    def __init__(self, entries):
        self.entries = tuple(entries)

    def snapshot(self):
        return self.entries


def _manifests():
    legacy = build_legacy_manifest({"slices": {"leaf": {}}}, run_id="run")
    candidate = build_plan_manifest(
        {
            "leaves": [
                {
                    "name": "leaf",
                    "task": "implement the leaf",
                    "agent_type": "opencode",
                    "boundary": ["src"],
                }
            ]
        },
        scope_id="run",
        owned_branch="main",
    )
    return legacy, candidate


def _active_slice():
    return SimpleNamespace(
        id="leaf",
        status=SliceStatus.IN_REVIEW,
        manifest_node_id="run/worker/leaf",
        dispatch_intent_id="spawn-intent",
        dispatch_invocation_id="invocation",
        dispatch_agent_id="agent",
        branch="main.leaf-opencode",
        worktree="/workspace/leaf",
        pr_number=43,
        reviewed_head="head-a",
        publication=PublicationBinding(43, "head-a", "main.leaf-opencode", "main", 1, "invocation"),
        handoff=HandoffEvidence(43, "head-a", 1, "invocation", "agent", "2026-09-03T00:00:00Z"),
        review_evidence=DurableReviewEvidence(
            7, 43, "head-a", "reviewer", Verdict.GO, "2026-09-03T00:00:00Z", "2026-09-03T00:01:00Z"
        ),
        action=ActionState(
            ActionKind.MERGE, ActionPhase.IN_FLIGHT, intent_id="merge-intent", head_sha="head-a"
        ),
    )


def _journal():
    return SnapshotJournal(
        [
            {
                "operation": "spawn_leaf",
                "target": "leaf",
                "status": "confirmed",
                "arguments": {
                    "name": "leaf",
                    "task": "implement the leaf",
                    "agent_type": "opencode",
                    "boundary": ["src"],
                    "intent_id": "spawn-intent",
                },
                "result": {
                    "success": True,
                    "result": {
                        "branch_name": "main.leaf-opencode",
                        "worktree_path": "/workspace/leaf",
                    },
                },
            },
            {
                "operation": "merge_pr",
                "target": "leaf",
                "key": "merge-intent",
                "status": "intended",
                "arguments": {"pr_number": 43, "expected_head_sha": "head-a"},
            },
        ]
    )


def test_active_legacy_binding_requires_and_records_authoritative_evidence():
    legacy, candidate = _manifests()
    result = reconcile_legacy_manifest(
        legacy,
        candidate,
        SimpleNamespace(slices={"leaf": _active_slice()}),
        _journal(),
    )

    assert result.disposition is LegacyManifestDisposition.PROVEN
    assert result.bindings == {"run/worker/leaf": "run/leaf/leaf"}
    proof = result.proofs[0]
    assert proof.branch == "main.leaf-opencode"
    assert "confirmed spawn" in proof.evidence


def test_active_legacy_binding_fails_closed_without_spawn_journal():
    legacy, candidate = _manifests()
    result = reconcile_legacy_manifest(
        legacy,
        candidate,
        SimpleNamespace(slices={"leaf": _active_slice()}),
        SnapshotJournal([]),
    )

    assert result.disposition is LegacyManifestDisposition.MISSING_EVIDENCE
    assert "confirmed spawn operation" in result.reason


def test_active_legacy_binding_rejects_conflicting_branch_evidence():
    legacy, candidate = _manifests()
    entries = list(_journal().entries)
    entries[0] = {
        **entries[0],
        "result": {"success": True, "result": {"branch_name": "main.other"}},
    }
    result = reconcile_legacy_manifest(
        legacy,
        candidate,
        SimpleNamespace(slices={"leaf": _active_slice()}),
        SnapshotJournal(entries),
    )

    assert result.disposition is LegacyManifestDisposition.CONFLICTING_EVIDENCE
    assert "owned branch" in result.reason


def test_migration_proof_projection_is_body_free():
    from tl_loop.state.diagnostics import project_legacy_manifest_migration

    projected = project_legacy_manifest_migration(
        {
            "old_node_id": "run/worker/leaf",
            "new_node_id": "run/leaf/leaf",
            "slice_id": "leaf",
            "disposition": "proven",
            "evidence": ["complete declaration"],
            "candidate_body": "secret",
        }
    )

    assert projected is not None
    assert "candidate_body" not in projected
    assert projected["disposition"] == "proven"


def test_migration_proof_round_trips_through_the_slice_schema(tmp_path):
    from tl_loop.state.plan_manifest import build_plan_manifest
    from tl_loop.state.store import create, RunStore

    manifest = build_plan_manifest(
        {"workers": [{"name": "worker", "task": "work"}]}, scope_id="schema"
    )
    create(
        "schema",
        {
            "plan_manifest": manifest.to_document(),
            "slices": {
                "worker": {
                    "id": "worker",
                    "status": "pending",
                    "paths": ["."],
                    "depends_on": [],
                    "base_ref": None,
                    "test_plan": [],
                    "agent_type": "opencode",
                    "model": None,
                    "branch": None,
                    "worktree": None,
                    "pr_number": None,
                    "reviewed_head": None,
                    "attempts": 0,
                    "verdict": None,
                    "manifest_node_id": "schema/worker/worker",
                    "manifest_revision": 1,
                    "legacy_manifest_migration": {
                        "old_node_id": "schema/legacy/worker",
                        "new_node_id": "schema/worker/worker",
                        "slice_id": "worker",
                        "disposition": "proven",
                        "evidence": ["exact scope/name and undispatched state"],
                        "missing": [],
                        "conflicts": [],
                        "branch": None,
                        "worktree": None,
                    },
                }
            },
        },
        root_dir=tmp_path,
    )
    loaded = RunStore("schema", tmp_path).load()
    assert loaded.slices["worker"].legacy_manifest_migration["disposition"] == "proven"
