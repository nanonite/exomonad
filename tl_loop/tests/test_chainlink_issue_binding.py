"""Contracts for optional, slice-scoped Chainlink issue bindings."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from dataclasses import replace

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.fsm.lane import LanePhase, LaneState
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.loop.driver import (
    CHAINLINK_BINDING_RECONCILIATION_GATE_NAME,
    ChainlinkIssueBindingError,
    TLLoopConfig,
    TLLoopError,
    WorkPlan,
    _advance_post_merge_boundary,
    _child_config,
    run_tl_loop,
    tl_run,
)
from tl_loop.rlm.plan_authoring import slices_to_plan_document
from tl_loop.rlm.slice_spec import SliceSpec
from tl_loop.state.plan_manifest import PlanManifest, build_plan_manifest
from tl_loop.state.schema import RepositoryIdentity, SliceStatus
from tl_loop.state.store import RunStore, create


def test_binding_round_trips_through_work_plan_and_recursive_manifest() -> None:
    document = {
        "sub_tls": [
            {
                "name": "stage",
                "order": 1,
                "plan": {
                    "leaves": [
                        {
                            "name": "leaf",
                            "task": "implement work",
                            "chainlink_issue_id": 1167,
                        }
                    ]
                },
            }
        ]
    }

    plan = WorkPlan.from_mapping(document)
    manifest = build_plan_manifest(document, scope_id="root")
    restored = PlanManifest.from_document(manifest.to_document())
    reconstructed = WorkPlan.from_mapping(
        {
            "sub_tls": [
                {
                    "name": "stage",
                    "order": 1,
                    "plan": {"leaves": [{"name": "leaf", "task": "implement work"}]},
                }
            ]
        }
    )

    assert plan.sub_tls[0].plan.leaves[0].chainlink_issue_id == 1167
    assert restored.child_manifests["root/sub_tl/stage"].node(
        "root/sub_tl/stage/leaf/leaf"
    ).chainlink_issue_id == 1167
    assert reconstructed.sub_tls[0].plan.leaves[0].chainlink_issue_id is None


@pytest.mark.parametrize("value", [0, -1, True, 1167.0, "1167"])
def test_binding_rejects_non_positive_or_non_integer_values(value: object) -> None:
    with pytest.raises(ChainlinkIssueBindingError, match="chainlink_issue_id"):
        WorkPlan.from_mapping(
            {"leaves": [{"name": "leaf", "task": "work", "chainlink_issue_id": value}]}
        )


def test_binding_rejects_duplicate_ids_across_recursive_slices() -> None:
    with pytest.raises(ChainlinkIssueBindingError, match="bind each issue to one slice"):
        WorkPlan.from_mapping(
            {
                "leaves": [
                    {"name": "one", "task": "one", "chainlink_issue_id": 91}
                ],
                "sub_tls": [
                    {
                        "name": "stage",
                        "order": 1,
                        "plan": {
                            "leaves": [
                                {"name": "two", "task": "two", "chainlink_issue_id": 91}
                            ]
                        },
                    }
                ],
            }
        )


def test_malformed_binding_opens_reconciliation_gate(tmp_path: Path) -> None:
    config = TLLoopConfig(root_dir=tmp_path, run_id="invalid-binding")

    with pytest.raises(TLLoopError, match=CHAINLINK_BINDING_RECONCILIATION_GATE_NAME):
        tl_run(
            {
                "run_id": "invalid-binding",
                "plan": {
                    "leaves": [
                        {"name": "leaf", "task": "work", "chainlink_issue_id": 0}
                    ]
                },
            },
            config,
            {"tokens": 0, "wall_seconds": 0},
        )

    state = RunStore("invalid-binding", tmp_path).load()
    assert any(
        gate.name == CHAINLINK_BINDING_RECONCILIATION_GATE_NAME
        for gate in state.gates
    )


def test_direct_loop_entrypoint_gates_malformed_binding(tmp_path: Path) -> None:
    class Transport:
        def call_tool(self, **arguments: object) -> dict[str, object]:
            del arguments
            raise AssertionError("invalid plans must be rejected before effects")

    config = TLLoopConfig(root_dir=tmp_path, run_id="direct-invalid-binding")
    with pytest.raises(TLLoopError, match=CHAINLINK_BINDING_RECONCILIATION_GATE_NAME):
        run_tl_loop(
            "direct-invalid-binding",
            {"leaves": [{"name": "leaf", "task": "work", "chainlink_issue_id": True}]},
            object(),
            EffectClient(Transport()),
            config=config,
            root_dir=tmp_path,
        )

    state = RunStore("direct-invalid-binding", tmp_path).load()
    assert any(
        gate.name == CHAINLINK_BINDING_RECONCILIATION_GATE_NAME
        for gate in state.gates
    )


def test_old_manifest_and_checkpoint_decode_as_unlinked(tmp_path: Path) -> None:
    manifest = build_plan_manifest(
        {"leaves": [{"name": "leaf", "task": "work"}]},
        scope_id="old-run",
    )
    node = manifest.node("old-run/leaf/leaf")
    create("old-run", {"plan_manifest": manifest.to_document()}, root_dir=tmp_path)

    restored = RunStore("old-run", tmp_path).load()

    assert "chainlink_issue_id" not in node.to_document()
    assert restored.slices["leaf"].chainlink_issue_id is None
    assert restored.plan_manifest == manifest


def test_unlinked_post_merge_persists_not_applicable_without_effects(tmp_path: Path) -> None:
    class Transport:
        calls: list[tuple[str, dict[str, object]]]

        def __init__(self) -> None:
            self.calls = []

        def call_tool(self, role: str, name: str, tool_name: str, arguments: dict[str, object]):
            del role, name
            self.calls.append((tool_name, arguments))
            raise AssertionError("unlinked completion must make no effect calls")

    plan = {"leaves": [{"name": "leaf", "task": "work"}]}
    manifest = build_plan_manifest(plan, scope_id="unlinked")
    identity = RepositoryIdentity("org", "repo", "main")
    create(
        "unlinked",
        {
            "plan_manifest": manifest.to_document(),
            "repository_identity": {
                "owner": identity.owner,
                "repo": identity.repo,
                "base_branch": identity.base_branch,
            },
        },
        root_dir=tmp_path,
    )
    store = RunStore("unlinked", tmp_path)
    state = store.load()
    current = replace(
        state.slices["leaf"],
        status=SliceStatus.MERGED,
        base_ref="main",
        pr_number=42,
        post_merge=PostMergeState(
            PostMergePhase.PARENT_BRANCH_SYNCED,
            {
                "child_id": "leaf",
                "repository": "org/repo",
                "parent_branch": "main",
                "pr_number": "42",
                "head_sha": "head-a",
                "merge_journal_id": "merge-journal",
                "lane_epoch": "1",
                "parent_commit_sha": "parent-commit",
            },
        ),
    )
    integration = replace(
        state.integration,
        lanes={
            "org/repo:main": LaneState(
                "org/repo",
                "main",
                phase=LanePhase.INTEGRATING,
                child_id="leaf",
                lane_epoch=1,
                head_sha="head-a",
            )
        },
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "leaf": current},
        state.budgets,
        state.events.last_consumed_offset,
        integration=integration,
        plan_manifest=manifest,
    )
    transport = Transport()

    completed = _advance_post_merge_boundary(
        state,
        "leaf",
        42,
        TLLoopConfig(repository_identity=identity),
        EffectClient(transport),
        [],
        store,
    )

    final = completed.slices["leaf"].post_merge
    assert final is not None and final.phase is PostMergePhase.COMPLETE
    assert final.evidence["issue_close_outcome"] == "not_applicable"
    assert final.evidence["changelog_outcome"] == "not_applicable"
    assert completed.integration.lanes["org/repo:main"].phase is LanePhase.IDLE
    assert transport.calls == []


def test_child_controller_config_has_no_parent_slice_issue_binding(tmp_path: Path) -> None:
    parent_plan = WorkPlan.from_mapping(
        {
            "sub_tls": [
                {
                    "name": "child",
                    "order": 1,
                    "chainlink_issue_id": 318,
                    "plan": {"leaves": [{"name": "leaf", "task": "child work"}]},
                }
            ]
        }
    )
    create("parent", {}, root_dir=tmp_path)
    store = RunStore("parent", tmp_path)
    child = _child_config(
        TLLoopConfig(root_dir=tmp_path),
        parent_plan.sub_tls[0],
        object(),
        object(),
        store,
        "main.child",
        str(tmp_path / "child"),
    )

    assert parent_plan.sub_tls[0].chainlink_issue_id == 318
    assert "chainlink_issue_id" not in {field.name for field in fields(TLLoopConfig)}
    assert not hasattr(child, "chainlink_issue_id")


def test_plan_authoring_preserves_explicit_slice_binding() -> None:
    authored = SliceSpec(
        id="leaf",
        title="implement work",
        paths=("src/leaf.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just tl-loop-test",),
        steps=("implement",),
        verify=("just tl-loop-test",),
        boundary=("src/leaf.py",),
        done_criteria=("work is complete",),
        chainlink_issue_id=702,
    )

    document = slices_to_plan_document([authored], run_id="authored")

    assert document["plan"]["leaves"][0]["chainlink_issue_id"] == 702
