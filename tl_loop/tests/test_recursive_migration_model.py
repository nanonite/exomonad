"""Independent migration matrix for recursive checkpoint compatibility."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tl_loop.state.migration import MigrationError, migrate_checkpoint_document
from tl_loop.state.plan_manifest import PlanManifest
from tl_loop.state.store import RunStore


def _legacy_checkpoint(
    slices: dict[str, dict[str, object]],
    *,
    phase: str = "tl_planning",
    ordered_stages: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    waiting_phases = {"tl_running", "tl_waiting", "tl_merging"}
    waiting = (
        [
            slice_id
            for slice_id, raw in slices.items()
            if raw.get("state", raw.get("status")) in {"spawned", "in_review", "repairing"}
            or (
                raw.get("state", raw.get("status")) == "merged"
                and isinstance(raw.get("post_merge"), dict)
                and raw["post_merge"].get("phase") != "complete"
            )
        ]
        if phase in waiting_phases
        else []
    )
    document: dict[str, object] = {
        "version": 3,
        "revision": 7,
        "run_id": "legacy-recursive",
        "fsm": {
            "phase": phase,
            "waiting": waiting,
        },
        "slices": slices,
        "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        "gates": [],
        "events": {"last_consumed_offset": 19},
    }
    if ordered_stages is not None:
        document["ordered_stages"] = ordered_stages
    return document


def _slice(status: str = "pending", **fields: object) -> dict[str, object]:
    return {"state": status, "paths": ["."], **fields}


def _manifest(result: object) -> PlanManifest:
    assert hasattr(result, "document")
    document = result.document  # type: ignore[attr-defined]
    return PlanManifest.from_document(document["plan_manifest"])  # type: ignore[arg-type]


def test_flat_parallel_legacy_plan_binds_every_slice() -> None:
    legacy = _legacy_checkpoint(
        {
            "worker-a": _slice(),
            "worker-b": _slice("completed"),
            "leaf-a": _slice("pending"),
        }
    )

    result = migrate_checkpoint_document(legacy, run_id="legacy-recursive")
    manifest = _manifest(result)

    assert {node.name for node in manifest.nodes} == {"worker-a", "worker-b", "leaf-a"}
    assert all(node.kind == "legacy" for node in manifest.nodes)
    assert result.document["gates"]
    for slice_id, state in result.document["slices"].items():  # type: ignore[union-attr]
        node = manifest.node(state["manifest_node_id"])  # type: ignore[index]
        assert node.name == slice_id
        assert state["manifest_revision"] == manifest.manifest_revision  # type: ignore[index]


def test_ordered_legacy_plan_preserves_barriers_and_source_order() -> None:
    legacy = _legacy_checkpoint(
        {
            "direct-worker": _slice(),
            "stage-a": _slice(),
            "stage-b": _slice(),
            "stage-c": _slice(),
        },
        ordered_stages=[
            {"order": 1, "sub_tls": ["stage-a", "stage-b"]},
            {"order": 2, "sub_tls": ["stage-c"]},
        ],
    )

    manifest = _manifest(migrate_checkpoint_document(legacy, run_id="legacy-recursive"))

    assert manifest.ordered_stages == (
        (1, ("legacy-recursive/sub_tl/stage-a", "legacy-recursive/sub_tl/stage-b")),
        (2, ("legacy-recursive/sub_tl/stage-c",)),
    )
    ordered_nodes = [
        manifest.node(node_id) for _, ids in manifest.ordered_stages for node_id in ids
    ]
    assert [node.name for node in ordered_nodes] == ["stage-a", "stage-b", "stage-c"]
    assert [node.order for node in ordered_nodes] == [1, 1, 2]
    assert all(node.kind == "sub_tl" for node in ordered_nodes)
    assert manifest.node("legacy-recursive/worker/direct-worker").kind == "legacy"


def test_nested_legacy_scope_fails_closed_instead_of_flattening() -> None:
    legacy = _legacy_checkpoint({"parent": _slice(sub_tls=[{"name": "child", "state": "pending"}])})

    with pytest.raises(MigrationError, match="nested scope.*cannot be reconstructed"):
        migrate_checkpoint_document(legacy, run_id="legacy-recursive")


def test_active_partial_legacy_run_preserves_dispatch_and_completion_evidence() -> None:
    legacy = _legacy_checkpoint(
        {
            "active-worker": _slice(
                "spawned",
                dispatch_intent_id="dispatch-intent",
                dispatch_agent_id="worker-agent",
                dispatch_authoritative_event_seq=12,
            ),
            "merged-leaf": _slice(
                "merged",
                pr_number=43,
                reviewed_head="head-a",
                verdict="GO",
            ),
        },
        phase="tl_running",
    )

    result = migrate_checkpoint_document(legacy, run_id="legacy-recursive")

    assert result.document["slices"]["active-worker"]["status"] == "spawned"  # type: ignore[index]
    assert result.document["slices"]["active-worker"]["dispatch_intent_id"] == "dispatch-intent"  # type: ignore[index]
    assert result.document["slices"]["merged-leaf"]["status"] == "merged"  # type: ignore[index]
    assert result.document["slices"]["merged-leaf"]["reviewed_head"] == "head-a"  # type: ignore[index]
    assert all(
        state["manifest_node_id"]
        for state in result.document["slices"].values()  # type: ignore[union-attr]
    )


def test_terminal_legacy_run_migrates_to_an_empty_authoritative_manifest() -> None:
    legacy = _legacy_checkpoint({}, phase="tl_done")

    first = migrate_checkpoint_document(legacy, run_id="legacy-recursive")
    second = migrate_checkpoint_document(copy.deepcopy(legacy), run_id="legacy-recursive")

    assert _manifest(first).nodes == ()
    assert first.document["gates"] == []
    assert first.document == second.document


@pytest.mark.parametrize(
    ("phase", "status"),
    [
        ("tl_planning", "pending"),
        ("tl_running", "spawned"),
        ("tl_dispatching", "spawned"),
        ("tl_waiting", "spawned"),
        ("tl_merging", "merged"),
        ("tl_all_merged", "merged"),
        ("tl_finalizing", "merged"),
        ("tl_pr_filed", "merged"),
        ("tl_failed", "failed"),
        ("tl_parked", "parked"),
    ],
)
def test_every_recursive_legacy_phase_migrates_with_its_slice_binding(
    phase: str, status: str
) -> None:
    legacy = _legacy_checkpoint({"child": _slice(status)}, phase=phase)

    result = migrate_checkpoint_document(legacy, run_id="legacy-recursive")
    manifest = _manifest(result)

    assert result.document["fsm"]["phase"] == phase  # type: ignore[index]
    migrated_slice = result.document["slices"]["child"]  # type: ignore[index]
    assert migrated_slice["manifest_node_id"] == manifest.nodes[0].node_id  # type: ignore[index]
    assert migrated_slice["manifest_revision"] == manifest.manifest_revision  # type: ignore[index]


@pytest.mark.parametrize(
    ("phase", "status"),
    [
        ("tl_planning", "pending"),
        ("tl_dispatching", "spawned"),
        ("tl_running", "in_review"),
        ("tl_waiting", "in_review"),
        ("tl_merging", "in_review"),
        ("tl_all_merged", "merged"),
        ("tl_finalizing", "merged"),
        ("tl_pr_filed", "merged"),
        ("tl_done", "merged"),
        ("tl_failed", "failed"),
        ("tl_parked", "parked"),
    ],
)
def test_phase_valid_legacy_fixture_loads_through_run_store(
    tmp_path: Path, phase: str, status: str
) -> None:
    legacy = _legacy_checkpoint({"child": _slice(status)}, phase=phase)
    run_dir = tmp_path / "legacy-store"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps(legacy), encoding="utf-8")

    state = RunStore("legacy-store", root_dir=tmp_path).load()

    assert state.fsm.phase.value == phase
    assert state.fsm.waiting == (
        ("child",) if phase in {"tl_running", "tl_waiting", "tl_merging"} else ()
    )


@pytest.mark.parametrize(
    ("ordered_stages", "message"),
    [
        ([{"order": 1, "sub_tls": ["missing"]}], "absent from slices"),
        ([{"order": 1, "sub_tls": ["stage"]}, {"order": 2, "sub_tls": ["stage"]}], "repeat"),
    ],
)
def test_irrecoverable_ordered_legacy_state_is_actionably_rejected(
    ordered_stages: list[dict[str, object]], message: str
) -> None:
    legacy = _legacy_checkpoint(
        {"stage": _slice()},
        ordered_stages=ordered_stages,
    )

    with pytest.raises(MigrationError, match=message):
        migrate_checkpoint_document(legacy, run_id="legacy-recursive")
