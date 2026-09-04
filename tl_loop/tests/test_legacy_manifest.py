"""Regression coverage for evidence-bound active manifest migration."""

from types import SimpleNamespace

from tl_loop.loop.review import ReviewContract
from tl_loop.state.legacy_manifest import (
    LegacyManifestDisposition,
    reconcile_legacy_manifest,
)
from tl_loop.state.plan_manifest import build_legacy_manifest, build_plan_manifest
from tl_loop.state.schema import (
    ActionKind,
    ActionPhase,
    ActionState,
    DurableReviewEvidence,
    HandoffEvidence,
    PublicationBinding,
    SliceStatus,
    Verdict,
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


def test_active_legacy_binding_rejects_candidate_nodes_not_in_old_manifest():
    legacy, _ = _manifests()
    candidate = build_plan_manifest(
        {
            "leaves": [
                {
                    "name": "leaf",
                    "task": "implement the leaf",
                    "agent_type": "opencode",
                    "boundary": ["src"],
                },
                {
                    "name": "extra",
                    "task": "do unrelated work",
                    "agent_type": "opencode",
                    "boundary": ["other"],
                },
            ]
        },
        scope_id="run",
        owned_branch="main",
    )
    result = reconcile_legacy_manifest(
        legacy,
        candidate,
        SimpleNamespace(slices={"leaf": _active_slice()}),
        _journal(),
    )

    assert result.disposition is LegacyManifestDisposition.CONFLICTING_EVIDENCE
    assert "extra" in result.reason
    assert result.proofs[0].new_node_id == "run/leaf/extra"


def test_active_legacy_binding_proves_the_complete_leaf_declaration():
    candidate = build_plan_manifest(
        {
            "leaves": [
                {
                    "name": "leaf",
                    "task": "implement the leaf",
                    "agent_type": "opencode",
                    "boundary": ["src"],
                    "context": "preserve the contract",
                    "read_first": ["README.md"],
                    "steps": ["implement", "verify"],
                    "verify": ["pytest"],
                    "done_criteria": ["all tests pass"],
                }
            ]
        },
        scope_id="run",
        owned_branch="main",
    )
    state = _active_slice()
    state.review_contract = ReviewContract.from_criteria(
        ("DONE CRITERIA: all tests pass",)
    ).as_mapping()
    entries = list(_journal().entries)
    entries[0]["arguments"].update(
        {
            "context": "preserve the contract",
            "read_first": ["README.md"],
            "steps": ["implement", "verify"],
            "verify": ["pytest"],
        }
    )
    legacy, _ = _manifests()
    result = reconcile_legacy_manifest(
        legacy,
        candidate,
        SimpleNamespace(slices={"leaf": state}),
        SnapshotJournal(entries),
    )

    assert result.disposition is LegacyManifestDisposition.PROVEN

    changed = build_plan_manifest(
        {
            "leaves": [
                {
                    "name": "leaf",
                    "task": "implement the leaf",
                    "agent_type": "opencode",
                    "boundary": ["src"],
                    "context": "changed contract",
                    "read_first": ["README.md"],
                    "steps": ["implement", "verify"],
                    "verify": ["pytest"],
                    "done_criteria": ["all tests pass"],
                }
            ]
        },
        scope_id="run",
        owned_branch="main",
    )
    assert (
        "declaration context"
        in reconcile_legacy_manifest(
            legacy,
            changed,
            SimpleNamespace(slices={"leaf": state}),
            SnapshotJournal(entries),
        ).reason
    )


def test_active_legacy_binding_rejects_publication_from_another_branch():
    legacy, candidate = _manifests()
    state = _active_slice()
    state.publication = PublicationBinding(43, "head-a", "main.other", "main", 1, "invocation")
    result = reconcile_legacy_manifest(
        legacy,
        candidate,
        SimpleNamespace(slices={"leaf": state}),
        _journal(),
    )

    assert result.disposition is LegacyManifestDisposition.CONFLICTING_EVIDENCE
    assert "publication head branch" in result.reason


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


def test_migration_cleanup_removes_only_the_exact_resolved_gate(tmp_path):
    from tl_loop.loop.driver import _clear_resolved_migration_gate
    from tl_loop.state.legacy_manifest import LegacyManifestReconciliation
    from tl_loop.state.store import RunStore, create

    create(
        "gates",
        {
            "gates": [
                {
                    "name": "plan-manifest-migration:proven:resolved",
                    "status": "approved",
                },
                {
                    "name": "plan-manifest-migration:proven:unrelated",
                    "status": "pending",
                },
                {"name": "operator-review", "status": "pending"},
            ]
        },
        root_dir=tmp_path,
    )
    store = RunStore("gates", tmp_path)
    reconciliation = LegacyManifestReconciliation(
        LegacyManifestDisposition.PROVEN,
        {},
        (),
        "resolved",
    )

    updated = _clear_resolved_migration_gate(store, store.load(), reconciliation)

    assert [gate.name for gate in updated.gates] == [
        "plan-manifest-migration:proven:unrelated",
        "operator-review",
    ]


def test_active_legacy_binding_rejects_worker_versus_leaf_kind_mismatch() -> None:
    legacy, _ = _manifests()
    candidate = build_plan_manifest(
        {"workers": [{"name": "leaf", "task": "implement the leaf"}]},
        scope_id="run",
        owned_branch="main",
    )
    result = reconcile_legacy_manifest(
        legacy,
        candidate,
        SimpleNamespace(slices={"leaf": _active_slice()}),
        _journal(),
    )

    assert result.disposition is LegacyManifestDisposition.MISSING_EVIDENCE
    assert "confirmed spawn operation" in result.reason


def test_active_legacy_binding_rejects_sub_tl_without_nested_checkpoint(tmp_path) -> None:
    legacy, _ = _manifests()
    candidate = build_plan_manifest(
        {"sub_tls": [{"name": "leaf", "order": 1, "plan": {}}]},
        scope_id="run",
        owned_branch="main",
    )
    result = reconcile_legacy_manifest(
        legacy,
        candidate,
        SimpleNamespace(slices={"leaf": _active_slice()}),
        _journal(),
        child_checkpoint_root=tmp_path,
    )

    assert result.disposition is LegacyManifestDisposition.MISSING_EVIDENCE
    assert "nested child checkpoint" in result.reason


def test_active_legacy_binding_rejects_in_flight_merge_without_action_journal_entry() -> None:
    legacy, candidate = _manifests()
    entries = list(_journal().entries)[:1]  # confirmed spawn only, no merge_pr entry
    result = reconcile_legacy_manifest(
        legacy,
        candidate,
        SimpleNamespace(slices={"leaf": _active_slice()}),
        SnapshotJournal(entries),
    )

    assert result.disposition is LegacyManifestDisposition.MISSING_EVIDENCE
    assert "matching action journal entry" in result.reason


def test_migration_proof_round_trips_through_the_slice_schema(tmp_path):
    from tl_loop.state.plan_manifest import build_plan_manifest
    from tl_loop.state.store import RunStore, create

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
