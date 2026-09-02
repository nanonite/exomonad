"""Contract tests for immutable recursive plan manifests."""

from __future__ import annotations

import queue

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.fsm.child import ChildKind, ChildRecord
from tl_loop.fsm.scope import TLPlanning, TLRunning
from tl_loop.loop import driver
from tl_loop.loop.driver import TLLoopConfig, _work_plan_from_manifest, run_tl_loop
from tl_loop.state.migration import MigrationError, migrate_checkpoint_document
from tl_loop.state.plan_manifest import (
    ManifestError,
    PlanManifest,
    build_legacy_manifest,
    build_plan_manifest,
    validate_manifest_revision,
)
from tl_loop.state.store import RunStore, _decode_recursive_fsm, _encode_fsm, create


def _plan() -> dict[str, object]:
    return {
        "workers": [{"name": "prepare", "task": "prepare the workspace"}],
        "leaves": [{"name": "leaf-a", "task": "implement the change", "boundary": ["src"]}],
        "sub_tls": [
            {
                "name": "stage-a",
                "order": 1,
                "plan": {"workers": [], "leaves": [{"name": "nested", "task": "nested work"}]},
            },
            {
                "name": "stage-b",
                "order": 2,
                "plan": {"workers": [], "leaves": []},
            },
        ],
    }


def test_manifest_digest_is_stable_and_recursive_round_trip() -> None:
    manifest = build_plan_manifest(_plan(), scope_id="run-1", owned_branch="main")
    reordered = {
        "sub_tls": list(reversed(_plan()["sub_tls"])),
        "leaves": _plan()["leaves"],
        "workers": _plan()["workers"],
    }
    reordered_manifest = build_plan_manifest(reordered, scope_id="run-1", owned_branch="main")

    assert manifest.digest != reordered_manifest.digest
    assert PlanManifest.from_document(manifest.to_document()) == manifest
    assert manifest.ordered_stages[0][1][0].endswith("stage-a")
    assert manifest.node("run-1/sub_tl/stage-a").child_manifest_digest
    nested = manifest.child_manifests["run-1/sub_tl/stage-a"]
    assert nested.nodes[0].name == "nested"
    assert nested.nodes[0].task == "nested work"
    assert (
        manifest.to_document()["child_manifests"]["run-1/sub_tl/stage-a"]["nodes"][0]["task"]
        == "nested work"
    )
    restored = _work_plan_from_manifest(manifest)
    assert restored.sub_tls[0].plan.leaves[0].task == "nested work"


def test_nested_nodes_target_their_scope_owned_branch() -> None:
    manifest = build_plan_manifest(
        {
            "sub_tls": [
                {
                    "name": "stage",
                    "order": 1,
                    "plan": {"leaves": [{"name": "leaf", "task": "leaf task"}]},
                }
            ]
        },
        scope_id="root",
        owned_branch="main",
    )

    stage = manifest.node("root/sub_tl/stage")
    leaf = manifest.child_manifests[stage.node_id].node("root/sub_tl/stage/leaf/leaf")
    assert stage.owned_branch == "main.stage"
    assert stage.parent_integration_target == "main"
    assert leaf.owned_branch == "main.stage.leaf"
    assert leaf.parent_integration_target == "main.stage"


def test_manifest_declarations_are_deeply_immutable() -> None:
    plan = {
        "leaves": [
            {
                "name": "leaf",
                "task": "task",
                "steps": ["first"],
            }
        ]
    }
    manifest = build_plan_manifest(plan, scope_id="immutable")
    declaration = manifest.node("immutable/leaf/leaf").declaration

    plan["leaves"][0]["steps"].append("mutated input")
    assert declaration["steps"] == ("first",)
    with pytest.raises(TypeError):
        declaration["steps"][0] = "mutated manifest"

    document = manifest.to_document()
    document["nodes"][0]["declaration"]["steps"].append("mutated document")
    assert manifest.node("immutable/leaf/leaf").declaration["steps"] == ("first",)
    assert PlanManifest.from_document(manifest.to_document()) == manifest


def test_manifest_rejects_tampering_and_non_monotonic_revision() -> None:
    manifest = build_plan_manifest(_plan(), scope_id="run-1")
    document = manifest.to_document()
    document["nodes"][0]["owned_branch"] = "other"
    with pytest.raises(ManifestError):
        PlanManifest.from_document(document)

    candidate = build_plan_manifest(
        {
            **_plan(),
            "sub_tls": _plan()["sub_tls"] + [{"name": "stage-c", "order": 3, "plan": {}}],
        },
        scope_id="run-1",
        manifest_revision=2,
    )
    protected = {node.node_id for node in manifest.nodes}
    validate_manifest_revision(manifest, candidate, protected_node_ids=protected)

    changed = build_plan_manifest(
        {**_plan(), "workers": [{"name": "prepare", "task": "different"}]},
        scope_id="run-1",
        manifest_revision=2,
    )
    with pytest.raises(ManifestError):
        validate_manifest_revision(manifest, changed, protected_node_ids=protected)

    removed = build_plan_manifest(
        {"workers": [{"name": "prepare", "task": "prepare"}]},
        scope_id="run-1",
        manifest_revision=2,
    )
    with pytest.raises(ManifestError, match="additive-only"):
        validate_manifest_revision(manifest, removed, protected_node_ids=set())


def test_legacy_migration_binds_slices_and_opens_recovery_gate() -> None:
    legacy = {
        "version": 3,
        "revision": 4,
        "run_id": "legacy-run",
        "fsm": {"phase": "tl_planning", "waiting": []},
        "slices": {},
        "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        "gates": [],
        "events": {"last_consumed_offset": 0},
    }
    result = migrate_checkpoint_document(legacy, run_id="legacy-run")
    assert result.migrated
    assert result.document["version"] > legacy["version"]
    assert result.document["plan_manifest"]["digest"]
    assert result.document["gates"] == []

    active = {
        **legacy,
        "slices": {"worker": {"state": "pending", "paths": ["."]}},
    }
    active_result = migrate_checkpoint_document(active, run_id="legacy-run")
    assert active_result.document["slices"]["worker"]["manifest_node_id"]
    assert any(
        gate["name"].startswith("plan-manifest-migration:")
        for gate in active_result.document["gates"]
    )

    manifest = build_legacy_manifest({"slices": {"unknown": {}}}, run_id="legacy-run")
    assert manifest.nodes[0].kind == "legacy"
    assert manifest.ordered_stages == ()


def test_store_round_trip_requires_exact_slice_manifest_binding(tmp_path) -> None:
    manifest = build_plan_manifest(
        {"workers": [{"name": "worker", "task": "work"}]},
        scope_id="bound-run",
    )
    create(
        "bound-run",
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
                    "agent_type": None,
                    "model": None,
                    "branch": None,
                    "worktree": None,
                    "pr_number": None,
                    "review_findings": {},
                    "ci_state": {},
                    "reviewer_attempt": {},
                    "repair_attempts": 0,
                    "reviewed_head": None,
                    "attempts": 0,
                    "verdict": None,
                }
            },
        },
        root_dir=tmp_path,
    )
    state = RunStore("bound-run", tmp_path).load()
    bound = state.slices["worker"]
    assert bound.manifest_node_id == "bound-run/worker/worker"
    assert bound.manifest_revision == manifest.manifest_revision
    assert state.plan_manifest == manifest


def test_store_rejects_changed_external_plan_without_revision(tmp_path) -> None:
    original = build_plan_manifest(_plan(), scope_id="authority")
    changed = build_plan_manifest(
        {**_plan(), "sub_tls": list(reversed(_plan()["sub_tls"]))},
        scope_id="authority",
    )
    revised = build_plan_manifest(
        {**_plan(), "sub_tls": _plan()["sub_tls"] + [{"name": "stage-c", "order": 3, "plan": {}}]},
        scope_id="authority",
        manifest_revision=2,
    )
    create("authority", {"plan_manifest": original.to_document()}, root_dir=tmp_path)
    store = RunStore("authority", tmp_path)
    with pytest.raises(ManifestError, match="revision"):
        store.set_plan_manifest(changed)
    revised_state = store.set_plan_manifest(revised)
    assert revised_state.plan_manifest == revised
    assert set(revised_state.slices) == {node.name for node in revised.nodes}


def test_schema_rejects_manifest_nodes_without_slice_state() -> None:
    from tl_loop.state.schema import SchemaError, validate

    manifest = build_plan_manifest(
        {"workers": [{"name": "worker", "task": "work"}]},
        scope_id="missing-slice",
    )
    document = {
        "version": 4,
        "revision": 0,
        "run_id": "missing-slice",
        "fsm": {"phase": "tl_planning", "waiting": []},
        "slices": {},
        "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        "gates": [],
        "events": {"last_consumed_offset": 0},
        "plan_manifest": manifest.to_document(),
    }
    with pytest.raises(SchemaError, match="no persisted slice state"):
        validate(document)


def test_malformed_legacy_ordered_stage_fails_as_migration_error() -> None:
    legacy = {"version": 3, "slices": {"child": {"state": "pending"}}}
    legacy["ordered_stages"] = [{"order": 1, "sub_tls": 7}]
    with pytest.raises(MigrationError, match="sub_tls must be an array"):
        migrate_checkpoint_document(legacy, run_id="legacy-run")


def test_current_checkpoint_without_manifest_migrates_before_schema_validation() -> None:
    legacy = {
        "version": 4,
        "revision": 1,
        "run_id": "legacy-v4",
        "fsm": {"phase": "tl_planning", "waiting": []},
        "slices": {},
        "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        "gates": [],
        "events": {"last_consumed_offset": 0},
    }

    result = migrate_checkpoint_document(legacy, run_id="legacy-v4")

    assert result.migrated is True
    assert result.document["version"] == 4
    assert result.document["plan_manifest"]["owned_branch"] == "legacy"


def test_current_checkpoint_with_null_manifest_migrates_before_schema_validation() -> None:
    legacy = {
        "version": 4,
        "revision": 1,
        "run_id": "legacy-v4-null",
        "fsm": {"phase": "tl_planning", "waiting": []},
        "slices": {},
        "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        "gates": [],
        "events": {"last_consumed_offset": 0},
        "plan_manifest": None,
    }

    result = migrate_checkpoint_document(legacy, run_id="legacy-v4-null")

    assert result.migrated is True
    assert result.document["version"] == 4
    assert result.document["plan_manifest"]["owned_branch"] == "legacy"


def test_schema_rejects_current_checkpoint_without_manifest() -> None:
    from tl_loop.state.schema import SchemaError, validate

    document = {
        "version": 4,
        "revision": 0,
        "run_id": "missing-manifest",
        "fsm": {"phase": "tl_planning", "waiting": []},
        "slices": {},
        "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        "gates": [],
        "events": {"last_consumed_offset": 0},
    }

    with pytest.raises(SchemaError, match="plan_manifest.*required"):
        validate(document)


def test_manifest_digest_is_unchanged_by_mutating_serialized_copies() -> None:
    manifest = build_plan_manifest(
        {"leaves": [{"name": "leaf", "task": "task", "steps": ["step"]}]},
        scope_id="copy",
    )
    document = manifest.to_document()
    document["nodes"][0]["declaration"]["steps"][0] = "changed copy"

    assert (
        manifest.digest
        == build_plan_manifest(
            {"leaves": [{"name": "leaf", "task": "task", "steps": ["step"]}]},
            scope_id="copy",
        ).digest
    )


def test_recursive_fsm_payload_round_trips_without_projection_loss(tmp_path) -> None:
    fsm = TLPlanning(
        scope_path=("root", "stage-a"),
        plan_digest="manifest-digest",
        parallel_children=(ChildRecord("nested-leaf", ChildKind.LEAF),),
    )
    state = create("recursive-fsm", {"fsm": fsm}, root_dir=tmp_path)

    assert state.recursive_fsm == fsm
    raw = (tmp_path / "recursive-fsm" / "run.json").read_text(encoding="utf-8")
    assert '"kind": "recursive"' in raw
    assert '"nested-leaf"' in raw


def test_continuation_reconstructs_work_plan_without_external_plan(
    tmp_path,
    monkeypatch,
) -> None:
    manifest = build_plan_manifest(
        {
            "leaves": [{"name": "nested", "task": "persisted task"}],
            "sub_tls": [
                {
                    "name": "child-scope",
                    "order": 1,
                    "plan": {"leaves": [{"name": "deep", "task": "deep task"}]},
                }
            ],
        },
        scope_id="resume",
    )
    create("resume", {"plan_manifest": manifest.to_document()}, root_dir=tmp_path)

    class EmptySource:
        def get(self, timeout=None):
            raise queue.Empty

        def acknowledge(self, event):
            return event.run_seq

    class NoopTransport:
        def call_tool(self, role, name, tool_name, arguments):
            return {"success": True, "result": {}}

    monkeypatch.setattr(
        driver,
        "_run_loop",
        lambda run_id, work_plan, *args, **kwargs: work_plan,
    )
    monkeypatch.setattr(
        driver,
        "_run_sub_tls",
        lambda work_plan, state, *args, **kwargs: state,
    )
    result = run_tl_loop(
        "resume",
        None,
        EmptySource(),
        ReadOnlyEffectClient(EffectClient(NoopTransport(), role="tl", name="root")),
        config=TLLoopConfig(active=False, max_events=1, root_dir=tmp_path),
        root_dir=tmp_path,
    )

    assert result.leaves[0].task == "persisted task"
    assert result.sub_tls[0].plan.leaves[0].task == "deep task"


def test_continuation_rejects_reordered_external_plan(tmp_path) -> None:
    original = _plan()
    manifest = build_plan_manifest(original, scope_id="reordered")
    create("reordered", {"plan_manifest": manifest.to_document()}, root_dir=tmp_path)

    class EmptySource:
        def get(self, timeout=None):
            raise queue.Empty

        def acknowledge(self, event):
            return event.run_seq

    class NoopTransport:
        def call_tool(self, role, name, tool_name, arguments):
            return {"success": True, "result": {}}

    reordered = {
        "workers": original["workers"],
        "leaves": original["leaves"],
        "sub_tls": list(reversed(original["sub_tls"])),
    }
    with pytest.raises(driver.TLLoopError, match="increase plan_revision"):
        run_tl_loop(
            "reordered",
            reordered,
            EmptySource(),
            ReadOnlyEffectClient(EffectClient(NoopTransport(), role="tl", name="root")),
            config=TLLoopConfig(active=False, max_events=1, root_dir=tmp_path),
            root_dir=tmp_path,
        )


def test_continuation_rejects_changed_external_plan(tmp_path) -> None:
    original = _plan()
    manifest = build_plan_manifest(original, scope_id="changed")
    create("changed", {"plan_manifest": manifest.to_document()}, root_dir=tmp_path)

    class EmptySource:
        def get(self, timeout=None):
            raise queue.Empty

        def acknowledge(self, event):
            return event.run_seq

    class NoopTransport:
        def call_tool(self, role, name, tool_name, arguments):
            return {"success": True, "result": {}}

    changed = {
        "workers": original["workers"],
        "leaves": [{"name": "leaf", "task": "changed task"}],
        "sub_tls": original["sub_tls"],
    }
    with pytest.raises(driver.TLLoopError, match="increase plan_revision"):
        run_tl_loop(
            "changed",
            changed,
            EmptySource(),
            ReadOnlyEffectClient(EffectClient(NoopTransport(), role="tl", name="root")),
            config=TLLoopConfig(active=False, max_events=1, root_dir=tmp_path),
            root_dir=tmp_path,
        )


def test_recursive_running_fsm_preserves_dispatch_and_lane_payloads() -> None:
    child = ChildRecord(
        "stage-a",
        ChildKind.SUB_TL,
        dispatch_intent_id="dispatch-intent",
        invocation_id="invocation",
        evidence={"head": "head-sha"},
        lane_id="repo:parent",
    )
    fsm = TLRunning(
        current_order=1,
        pending_by_order={1: (child,)},
        scope_path=("root",),
        plan_digest="manifest-digest",
        dispatch_intents={"stage-a": "dispatch-intent"},
        evidence={"stage-a": "review-evidence"},
        lane_bindings={"stage-a": "repo:parent"},
    )

    assert _decode_recursive_fsm(_encode_fsm(fsm)) == fsm
