"""Direct-leaf dependency scheduling: edges, dispatch gating, and propagation."""

from __future__ import annotations

import json
import queue
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import cast

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import JsonObject
from tl_loop.events.envelope import EventEnvelope
from tl_loop.select.policy import HarnessPolicy, validate_policy
from tl_loop.loop import driver
from tl_loop.loop.driver import (
    EffectIntent,
    TLLoopConfig,
    WorkPlan,
    _bind_initial_slices,
    _block_unsatisfiable_dependents,
    _dispatch_children,
    _can_dispatch,
    _initial_slices,
    _rebind_dependency_edges,
    _reopens_dispatch_pass,
    _work_plan_from_manifest,
    run_tl_loop,
)
from tl_loop.loop.schedule import (
    dependencies_satisfied,
    ready,
    unsatisfiable_dependencies,
    withhold_unsatisfiable_dependents,
)
from tl_loop.plan_dependencies import DependencyValidationError, validate_leaf_dependencies
from tl_loop.plan_validation import PlanValidationError, validate_plan_document
from tl_loop.state.plan_manifest import (
    ManifestError,
    PlanManifest,
    build_legacy_manifest,
    build_plan_manifest,
    validate_manifest_revision,
)
from tl_loop.state.schema import RunState, SliceState, SliceStatus
from tl_loop.state.store import RunStore, create

from .test_driver import SyntheticQueue, _event


class _EmptySource:
    """A source with nothing to replay, so a run makes no scheduling progress."""

    def get(self, timeout: float | None = None) -> EventEnvelope:
        del timeout
        raise queue.Empty

    def acknowledge(self, event: EventEnvelope) -> int:
        del event
        return 0


# --------------------------------------------------------------------------
# Schema and same-scope DAG validation
# --------------------------------------------------------------------------


def test_plan_document_accepts_a_leaf_dag_and_keeps_the_edges() -> None:
    document = _plan_document(leaf_a(), leaf_b("a"), leaf_c("a", "b"))

    leaves = cast(list[dict[str, object]], validate_plan_document(document)["plan"]["leaves"])  # type: ignore[index]

    assert [leaf.get("depends_on", []) for leaf in leaves] == [[], ["a"], ["a", "b"]]


def test_missing_self_cycle_and_incompatible_targets_are_rejected() -> None:
    with pytest.raises(DependencyValidationError, match="unknown sibling 'ghost'"):
        _parse(leaf_a(), leaf_b("ghost"))
    with pytest.raises(DependencyValidationError, match="depends_on itself"):
        _parse(leaf_a("a"))
    with pytest.raises(DependencyValidationError, match="cycle: a -> b -> a"):
        _parse(leaf_a("b"), leaf_b("a"))
    with pytest.raises(DependencyValidationError, match="not a direct leaf"):
        _parse_workers(leaf_b("w"), workers=({"name": "w", "task": "prepare"},))
    with pytest.raises(DependencyValidationError, match="not a direct leaf"):
        _parse_sub_tls(leaf_b("nested"))


def test_edges_are_unique_and_never_inferred_from_paths_or_list_order() -> None:
    with pytest.raises(DependencyValidationError, match="repeats 'a'"):
        _parse(leaf_a(), leaf_b("a", "a"))

    # Two leaves that overlap on paths declare no edge, so none is invented.
    overlapping = _leaves(
        leaf_a(boundary=("src/shared/**",)),
        leaf_b("a", boundary=("src/shared/api.py",)),
    )
    assert [leaf.get("depends_on", []) for leaf in overlapping] == [[], ["a"]]

    # Declaration order carries no meaning: reversing the list reverses nothing.
    reversed_leaves = _leaves(leaf_b("a"), leaf_a())
    assert [leaf.get("depends_on", []) for leaf in reversed_leaves] == [["a"], []]


def test_a_nested_scope_resolves_its_edges_within_its_own_plan() -> None:
    document = {
        "run_id": "root",
        "plan": {
            "sub_tls": [{"name": "nested", "order": 1, "plan": {"leaves": [leaf_a(), leaf_b("a")]}}]
        },
    }

    validated = validate_plan_document(document)
    nested = cast(dict[str, object], validated["plan"]["sub_tls"][0]["plan"])  # type: ignore[index]

    assert [leaf.get("depends_on", []) for leaf in cast(list[dict[str, object]], nested["leaves"])] == [  # type: ignore[index]
        [],
        ["a"],
    ]


# --------------------------------------------------------------------------
# Manifest persistence, identity, and revision safety
# --------------------------------------------------------------------------


def test_manifest_persists_edges_and_reconstructs_them_after_restart() -> None:
    plan = {"leaves": [leaf_a(), leaf_b("a"), leaf_c("a", "b")]}

    manifest = build_plan_manifest(plan, scope_id="root")
    restored = _work_plan_from_manifest(manifest)

    assert {node.name: node.depends_on for node in manifest.nodes} == {
        "a": (),
        "b": ("a",),
        "c": ("a", "b"),
    }
    assert {leaf.name: leaf.depends_on for leaf in restored.leaves} == {
        "a": (),
        "b": ("a",),
        "c": ("a", "b"),
    }
    assert build_plan_manifest(plan, scope_id="root").digest == manifest.digest


def test_a_manifest_edge_is_node_identity_so_a_revision_cannot_rewrite_it() -> None:
    previous = build_plan_manifest({"leaves": [leaf_a(), leaf_b()]}, scope_id="root")
    candidate = build_plan_manifest(
        {"leaves": [leaf_a(), leaf_b("a")]},
        scope_id="root",
        manifest_revision=2,
    )
    node_id = "root/leaf/b"

    assert previous.node(node_id).identity() != candidate.node(node_id).identity()
    with pytest.raises(ManifestError, match="mutates protected node"):
        validate_manifest_revision(previous, candidate, protected_node_ids={node_id})


def test_an_additive_revision_may_add_a_leaf_that_depends_on_an_existing_one() -> None:
    previous = build_plan_manifest({"leaves": [leaf_a(), leaf_b()]}, scope_id="root")
    candidate = build_plan_manifest(
        {"leaves": [leaf_a(), leaf_b("a"), leaf_c("a", "b")]},
        scope_id="root",
        manifest_revision=2,
    )

    validate_manifest_revision(previous, candidate, protected_node_ids=set())

    assert candidate.manifest_revision == 2
    assert candidate.node("root/leaf/c").depends_on == ("a", "b")
    assert previous.node("root/leaf/b").depends_on == ()


def test_manifest_rejects_an_edge_naming_a_non_leaf_or_an_unknown_node() -> None:
    with pytest.raises(ManifestError, match="not a direct leaf"):
        build_plan_manifest(
            {"workers": [{"name": "w", "task": "prepare"}], "leaves": [leaf_b("w")]},
            scope_id="root",
        )
    with pytest.raises(ManifestError, match="unknown sibling 'ghost'"):
        build_plan_manifest({"leaves": [leaf_b("ghost")]}, scope_id="root")


@pytest.mark.parametrize("kind", ["worker", "sub_tl"])
def test_manifest_refuses_an_edge_declared_by_a_kind_that_never_merges(kind: str) -> None:
    """An unmergeable kind's edge is rejected, never quietly dropped.

    Discarding the key would leave a manifest that reads as though no ordering
    had been requested, which is exactly the silent-early-dispatch failure the
    closed plan contract exists to prevent.
    """
    entry: dict[str, object] = (
        {"name": "w", "task": "prepare", "depends_on": ["a"]}
        if kind == "worker"
        else {"name": "nested", "order": 1, "depends_on": ["a"], "plan": {"leaves": []}}
    )
    key = "workers" if kind == "worker" else "sub_tls"

    with pytest.raises(ManifestError, match="cannot declare depends_on"):
        build_plan_manifest({key: [entry], "leaves": [leaf_a()]}, scope_id="root")


def test_a_legacy_checkpoint_carries_no_edges_and_still_loads() -> None:
    legacy = build_legacy_manifest(
        {"slices": {"one": {"status": "pending"}, "two": {"status": "pending"}}}, run_id="legacy"
    )

    assert all(node.depends_on == () for node in legacy.nodes)
    assert PlanManifest.from_document(legacy.to_document()) == legacy


#: The digest of a two-leaf, edge-free scope as produced *before* direct leaf
#: dependencies existed. A manifest is the durable authority an in-flight run
#: resumes from, so adding the field must not invalidate a document an earlier
#: controller already wrote. This is pinned rather than recomputed so a future
#: unconditional ``depends_on`` key in the node payload fails here.
_PRE_DEPENDENCY_MANIFEST_DIGEST = "b06d18fbfc7cfbdc0aec99269537d3e418c4f139f1cca549a304294be60f06a7"

#: A real manifest document written by that earlier serializer: no node carries
#: ``depends_on``, and its recorded digest predates the field.
_PRE_DEPENDENCY_MANIFEST = Path(__file__).parent / "fixtures/legacy/pre-dependency-manifest.json"


def test_an_edge_free_manifest_keeps_the_digest_an_earlier_controller_recorded() -> None:
    manifest = build_plan_manifest(_mapping(leaf_a(), leaf_b()), scope_id="root")

    assert manifest.digest == _PRE_DEPENDENCY_MANIFEST_DIGEST
    assert all("depends_on" not in node for node in _nodes(manifest))
    assert PlanManifest.from_document(manifest.to_document()) == manifest


def test_a_manifest_persisted_before_the_field_existed_still_resumes() -> None:
    """The document is the one an older controller wrote, verbatim.

    It carries no ``depends_on`` key on any node and a digest computed without
    one. Decoding it proves an in-flight run is not locked out by the upgrade.
    """
    document = json.loads(Path(_PRE_DEPENDENCY_MANIFEST).read_text(encoding="utf-8"))

    manifest = PlanManifest.from_document(document)

    assert manifest.digest == _PRE_DEPENDENCY_MANIFEST_DIGEST
    assert {node.name: node.depends_on for node in manifest.nodes} == {"a": (), "b": ()}


def test_a_scope_that_declares_edges_records_them_in_its_document() -> None:
    manifest = build_plan_manifest(_mapping(leaf_a(), leaf_b("a")), scope_id="root")

    declared = [node["depends_on"] for node in _nodes(manifest) if "depends_on" in node]
    assert declared == [["a"]]
    assert PlanManifest.from_document(manifest.to_document()) == manifest


# --------------------------------------------------------------------------
# Slice records carry the declared edges
# --------------------------------------------------------------------------


def test_initial_slice_records_carry_the_declared_edges() -> None:
    plan = _parse(leaf_a(), leaf_b("a"))

    records = _initial_slices(plan, TLLoopConfig())

    assert {name: record["depends_on"] for name, record in records.items()} == {
        "a": [],
        "b": ["a"],
    }


def test_a_node_added_by_a_manifest_revision_starts_with_its_declared_edges(
    tmp_path: Path,
) -> None:
    """A newly declared node's inert record already carries its edges.

    ``set_plan_manifest`` builds the record for a node the run has never seen,
    and that is the record dispatch reads. If it started edge-free the declared
    prerequisite would never gate the dependent.
    """
    store = RunStore(root_dir=tmp_path, run_id="accepted")
    create(
        "accepted",
        {
            "slices": {"a": _record("a")},
            "plan_manifest": build_plan_manifest(
                {"leaves": [leaf_a()]}, scope_id="accepted"
            ).to_document(),
        },
        root_dir=store.root_dir,
    )

    state = store.set_plan_manifest(
        build_plan_manifest(
            _mapping(leaf_a(), leaf_b("a")), scope_id="accepted", manifest_revision=2
        )
    )

    assert state.slices["b"].depends_on == ("a",)
    assert state.slices["b"].status is SliceStatus.PENDING


def test_a_worker_records_no_edge_because_it_never_merges() -> None:
    records = _initial_slices(_parse({"name": "w", "task": "prepare"}), TLLoopConfig())

    assert records["w"]["depends_on"] == []


# --------------------------------------------------------------------------
# Dispatch gating
# --------------------------------------------------------------------------


def test_a_dependent_waits_until_its_prerequisite_is_merged(tmp_path: Path) -> None:
    slices = {"a": _slice("a"), "b": _slice("b", depends_on=("a",))}
    state = _run_state(slices, tmp_path)

    assert dependencies_satisfied(slices, "b") is False
    assert _can_dispatch("a", state, TLLoopConfig()) is True
    assert _can_dispatch("b", state, TLLoopConfig()) is False
    assert state.slices["b"].depends_on == ("a",)
    assert [item.id for item in ready(slices)] == ["a"]

    slices["a"] = _slice("a", SliceStatus.SPAWNED)
    assert [item.id for item in ready(slices)] == []

    slices["a"] = _slice("a", SliceStatus.MERGED)
    assert dependencies_satisfied(slices, "b") is True
    assert _can_dispatch("b", _run_state(slices, tmp_path, run_id="merged"), TLLoopConfig()) is True
    assert [item.id for item in ready(slices)] == ["b"]


def test_an_edge_is_enforced_without_a_policy_or_a_width_ceiling(tmp_path: Path) -> None:
    """The edge is a plan fact, not a scheduling preference.

    A dependent leaf waits even for a run that configured neither a harness
    policy nor a parallel width, because an edge is a dispatch precondition
    rather than a resource-budget decision.
    """
    config = TLLoopConfig()
    state = _run_state({"a": _slice("a"), "b": _slice("b", depends_on=("a",))}, tmp_path)

    assert config.policy is None and config.max_parallel_slices is None
    assert _can_dispatch("a", state, config) is True
    assert _can_dispatch("b", state, config) is False


def test_a_chain_releases_one_slice_at_a_time() -> None:
    slices = {
        "a": _slice("a"),
        "b": _slice("b", depends_on=("a",)),
        "c": _slice("c", depends_on=("a", "b")),
    }

    assert [item.id for item in ready(slices)] == ["a"]
    slices["a"] = _slice("a", SliceStatus.MERGED)
    assert [item.id for item in ready(slices)] == ["b"]
    slices["b"] = _slice("b", SliceStatus.MERGED, depends_on=("a",))
    assert [item.id for item in ready(slices)] == ["c"]


def test_a_fan_of_dependents_releases_together_once_its_root_merges() -> None:
    slices = {
        "root": _slice("root"),
        "left": _slice("left", depends_on=("root",)),
        "right": _slice("right", depends_on=("root",)),
    }

    assert [item.id for item in ready(slices)] == ["root"]
    slices["root"] = _slice("root", SliceStatus.MERGED)
    assert [item.id for item in ready(slices)] == ["left", "right"]


# --------------------------------------------------------------------------
# Failure and human-gate propagation
# --------------------------------------------------------------------------


def test_a_failed_prerequisite_blocks_its_transitive_dependents() -> None:
    slices = {
        "a": _slice("a", SliceStatus.FAILED),
        "b": _slice("b", depends_on=("a",)),
        "c": _slice("c", depends_on=("b",)),
        "sibling": _slice("sibling"),
    }

    assert unsatisfiable_dependencies(slices, "b") == ("a",)
    blocked = withhold_unsatisfiable_dependents(slices)

    assert blocked["b"].status is SliceStatus.BLOCKED
    assert blocked["b"].blocked_by == "a"
    assert blocked["c"].status is SliceStatus.BLOCKED
    assert blocked["c"].blocked_by == "b"
    assert blocked["sibling"].status is SliceStatus.PENDING
    assert withhold_unsatisfiable_dependents(blocked) == blocked


def test_a_blocked_prerequisite_also_blocks_its_dependents() -> None:
    slices = {"a": _slice("a", SliceStatus.BLOCKED), "b": _slice("b", depends_on=("a",))}

    assert unsatisfiable_dependencies(slices, "b") == ("a",)


@pytest.mark.parametrize("gated", [SliceStatus.PARKED, SliceStatus.DISPATCH_FAILED])
def test_a_gated_prerequisite_holds_its_dependent_rather_than_blocking_it(
    gated: SliceStatus, tmp_path: Path
) -> None:
    """A gate is a question the operator may still answer, not a dead end."""
    slices = {"a": _slice("a", gated), "b": _slice("b", depends_on=("a",))}

    assert unsatisfiable_dependencies(slices, "b") == ()
    assert withhold_unsatisfiable_dependents(slices) == slices
    assert _can_dispatch("b", _run_state(slices, tmp_path), TLLoopConfig()) is False
    assert ready(slices) == []


def test_a_waiting_dependency_is_not_reported_as_a_schedule_deadlock() -> None:
    assert ready({"a": _slice("a", SliceStatus.PARKED), "b": _slice("b", depends_on=("a",))}) == []
    assert ready({"a": _slice("a", SliceStatus.SPAWNED), "b": _slice("b", depends_on=("a",))}) == []


def test_a_cycle_left_by_a_hand_edited_checkpoint_still_deadlocks() -> None:
    from tl_loop.loop.schedule import ScheduleDeadlock

    with pytest.raises(ScheduleDeadlock):
        ready({"a": _slice("a", depends_on=("b",)), "b": _slice("b", depends_on=("a",))})


def test_blocking_a_dependent_is_durable_and_idempotent(tmp_path: Path) -> None:
    store = RunStore(root_dir=tmp_path, run_id="propagation")
    create(
        "propagation",
        {
            "slices": {
                "a": _record("a", status=SliceStatus.FAILED),
                "b": _record("b", depends_on=["a"]),
            },
            "plan_manifest": build_plan_manifest(
                {"leaves": [leaf_a(), leaf_b("a")]}, scope_id="propagation"
            ).to_document(),
        },
        root_dir=store.root_dir,
    )
    effects_log: list[EffectIntent] = []

    def block() -> RunState:
        return _block_unsatisfiable_dependents(
            store.load(),
            TLLoopConfig(root_dir=tmp_path),
            EffectClient(_Transport()),  # type: ignore[arg-type]
            effects_log,
            store,
        )

    state = block()

    assert state.slices["b"].status is SliceStatus.BLOCKED
    assert state.slices["b"].blocked_by == "a"
    assert store.load().slices["b"].status is SliceStatus.BLOCKED
    assert block().slices["b"].status is SliceStatus.BLOCKED
    blocked = _blocked_events(effects_log)
    assert blocked == [
        {
            "slice_id": "b",
            "depends_on": ["a"],
            "blocked_by": "a",
            "reason": "a declared dependency can no longer merge",
        }
    ]


# --------------------------------------------------------------------------
# End-to-end dispatch ordering and restart
# --------------------------------------------------------------------------


def test_a_leaf_dag_dispatches_in_dependency_order(tmp_path: Path) -> None:
    """Only the DAG root is dispatched, and the dependent follows its merge."""
    plan, store, transport = _started(
        tmp_path, "leaf-dag-run", _mapping(leaf_a(), leaf_b("a"))
    )

    _dispatch_pass(plan, store, transport)
    assert _spawned(transport) == ["a"]

    _merge(store, "a")
    _dispatch_pass(plan, store, transport)

    assert _spawned(transport) == ["a", "b"]
    assert store.load().slices["b"].depends_on == ("a",)


def test_a_fan_out_dag_dispatches_every_dependent_after_its_root(
    tmp_path: Path,
) -> None:
    plan, store, transport = _started(
        tmp_path, "fan-run", _mapping(leaf_a(), leaf_b("a"), leaf_c("a"))
    )

    _dispatch_pass(plan, store, transport)
    assert _spawned(transport) == ["a"]

    _merge(store, "a")
    _dispatch_pass(plan, store, transport)

    assert _spawned(transport) == ["a", "b", "c"]


def test_a_chain_never_dispatches_past_an_unmerged_prerequisite(tmp_path: Path) -> None:
    plan, store, transport = _started(
        tmp_path, "chain-run", _mapping(leaf_a(), leaf_b("a"), leaf_c("a", "b"))
    )

    _dispatch_pass(plan, store, transport)
    _merge(store, "a")
    _dispatch_pass(plan, store, transport)

    assert _spawned(transport) == ["a", "b"]
    assert store.load().slices["c"].status is SliceStatus.PENDING


def test_a_dispatch_pass_reopens_when_a_dependency_merges(tmp_path: Path) -> None:
    """The pass reopens on the merge event, not only up front."""
    dependent = WorkPlan.from_mapping(
        {"leaves": [leaf_a(), leaf_b("a", verify=("just tl-loop-test",))]}
    )
    independent = WorkPlan.from_mapping(
        {"leaves": [leaf_a(), leaf_b(verify=("just tl-loop-test",))]}
    )

    assert _reopens_dispatch_pass(dependent, TLLoopConfig()) is True
    assert _reopens_dispatch_pass(independent, TLLoopConfig()) is False
    policy_config = TLLoopConfig(policy=_policy(), max_parallel_slices=1)
    assert _reopens_dispatch_pass(independent, policy_config) is True


def test_a_dependency_edge_survives_a_restart_from_the_persisted_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A continuation with no plan rebuilds the DAG from the durable manifest.

    The edge must survive because the manifest is the authority, not because
    ``plan.json`` was re-read, so this asserts the reconstructed executable
    plan the driver resumes from.
    """
    run_id = "restart-run"
    manifest = build_plan_manifest({"leaves": [leaf_a(), leaf_b("a")]}, scope_id=run_id)
    create(run_id, {"plan_manifest": manifest.to_document()}, root_dir=tmp_path)
    monkeypatch.setattr(driver, "_run_loop", lambda _run_id, plan, *a, **k: plan)

    result = run_tl_loop(
        run_id,
        None,
        _EmptySource(),
        EffectClient(_Transport()),
        config=TLLoopConfig(max_events=1, root_dir=tmp_path),
        root_dir=tmp_path,
    )

    assert isinstance(result, WorkPlan)
    assert {leaf.name: leaf.depends_on for leaf in result.leaves} == {"a": (), "b": ("a",)}
    persisted = RunStore(root_dir=tmp_path, run_id=run_id).load().plan_manifest
    assert persisted is not None
    assert persisted.digest == manifest.digest


def test_a_revision_rewires_a_pending_leaf_but_never_a_dispatched_one(
    tmp_path: Path,
) -> None:
    """Edges move only while no dispatch has acted on them.

    A leaf still in ``PENDING``/``READY`` has had nothing dispatched from its
    old edge, so a revision may rewire it and the slice record follows the
    manifest. A dispatched leaf has acted on its edge, and the revision check
    refuses to touch a protected node at all.
    """
    plan = {"leaves": [leaf_a(), leaf_b(), leaf_c()]}
    previous = build_plan_manifest(plan, scope_id="rewire", manifest_revision=1)
    rewired = build_plan_manifest(
        {"leaves": [leaf_a(), leaf_b("a"), leaf_c()]}, scope_id="rewire", manifest_revision=2
    )
    store = RunStore(root_dir=tmp_path, run_id="rewire")
    create(
        "rewire",
        {
            "slices": {"a": _record("a"), "b": _record("b"), "c": _record("c")},
            "plan_manifest": previous.to_document(),
        },
        root_dir=store.root_dir,
    )
    pending = store.load().slices

    rebound = _rebind_dependency_edges(
        {name: _encoded(pending)[name] for name in pending},
        pending,
        rewired,
    )

    assert rebound["b"]["depends_on"] == ["a"]

    dispatched = store.set_plan_manifest(
        rewired,
        slices={
            **_encoded(pending),
            "a": _record("a", status=SliceStatus.MERGED),
        },
        protected_node_ids={"rewire/leaf/a"},
    )
    assert dispatched.slices["a"].depends_on == ()
    rewire_dispatched = build_plan_manifest(
        {"leaves": [leaf_a(), leaf_b("c"), leaf_c()]}, scope_id="rewire", manifest_revision=3
    )
    with pytest.raises(ManifestError, match="mutates protected node 'rewire/leaf/b'"):
        store.set_plan_manifest(
            rewire_dispatched,
            slices={**_encoded(pending), "b": _record("b", status=SliceStatus.MERGED)},
            protected_node_ids={"rewire/leaf/b"},
        )


def test_rebinding_leaves_a_slice_the_manifest_does_not_declare_alone() -> None:
    """A legacy slice keeps whatever ordering its own writer gave it.

    A migrated checkpoint carries slices the manifest never declared. The
    manifest has no opinion about them, so the rebind must not answer for it
    and erase a recorded edge.
    """
    manifest = build_plan_manifest(_mapping(leaf_a(), leaf_b("a")), scope_id="rewire")
    undeclared = _record("stranger", depends_on=["someone"])

    rebound = _rebind_dependency_edges(
        {"a": _record("a"), "stranger": undeclared},
        {"a": _slice("a"), "stranger": _slice("stranger", depends_on=("someone",))},
        manifest,
    )

    assert rebound["stranger"] == undeclared


def test_sub_tl_stages_and_leaf_edges_stay_independent_contracts() -> None:
    document = {
        "run_id": "root",
        "plan": {
            "leaves": [leaf_a(), leaf_b("a")],
            "sub_tls": [
                {"name": "nested", "order": 1, "plan": {"leaves": [leaf_a(), leaf_b("a")]}}
            ],
        },
    }

    plan = WorkPlan.from_mapping(cast(dict[str, object], document["plan"]))

    assert plan.ordered_stages[0].order == 1
    nested = plan.sub_tls[0].plan
    assert isinstance(nested, WorkPlan)
    assert nested.leaves[1].depends_on == ("a",)
    assert plan.leaves[1].depends_on == ("a",)
    assert _initial_slices(plan, TLLoopConfig())["b"]["depends_on"] == ["a"]
    assert _initial_slices(plan, TLLoopConfig())["nested"]["depends_on"] == []


def test_a_rejected_dependency_edge_fails_the_run_before_any_dispatch(
    tmp_path: Path,
) -> None:
    transport = _Transport()

    with pytest.raises(DependencyValidationError, match="unknown sibling 'ghost'"):
        run_tl_loop(
            "rejected-dag",
            {"leaves": [leaf_a(), leaf_b("ghost")]},
            _EmptySource(),
            EffectClient(transport),
            config=TLLoopConfig(root_dir=tmp_path, max_events=1, poll_interval=0.001),
            root_dir=tmp_path,
        )

    assert transport.calls == []


def test_malformed_depends_on_values_are_rejected_as_a_closed_shape() -> None:
    with pytest.raises(DependencyValidationError, match="depends_on must be an array"):
        validate_leaf_dependencies(
            cast("dict[str, Sequence[object]]", {"a": "b"}),
            leaf_names={"a", "b"},
            other_names=frozenset(),
        )
    with pytest.raises(DependencyValidationError, match="must be a non-empty name"):
        validate_leaf_dependencies({"a": [""]}, leaf_names={"a"}, other_names=frozenset())
    with pytest.raises(PlanValidationError, match="depends_on"):
        validate_plan_document(
            {
                "run_id": "root",
                "plan": {"leaves": [{"name": "a", "task": "x", "depends_on": "b"}]},
            }
        )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def leaf_a(*depends_on: str, **overrides: object) -> dict[str, object]:
    return _leaf("a", "src/a.py", depends_on, **overrides)


def _leaf(
    name: str, path: str, depends_on: tuple[str, ...], **overrides: object
) -> dict[str, object]:
    """Build one JSON-shaped leaf entry, declaring edges only when there are any."""
    leaf: dict[str, object] = {"name": name, "task": f"implement {name}", "boundary": [path]}
    if depends_on:
        leaf["depends_on"] = list(depends_on)
    return {**leaf, **overrides}


def leaf_b(*depends_on: str, **overrides: object) -> dict[str, object]:
    return _leaf("b", "src/b.py", depends_on, **overrides)


def leaf_c(*depends_on: str, **overrides: object) -> dict[str, object]:
    return _leaf("c", "src/c.py", depends_on, **overrides)


def _plan_document(*children: dict[str, object]) -> dict[str, object]:
    return {"run_id": "root", "plan": {"leaves": list(children)}}


def _leaves(*children: dict[str, object]) -> list[dict[str, object]]:
    return cast(
        list[dict[str, object]],
        validate_plan_document(_plan_document(*children))["plan"]["leaves"],  # type: ignore[index]
    )


def _mapping(*children: dict[str, object]) -> dict[str, object]:
    return {"leaves": list(children)}


def _nodes(manifest: PlanManifest) -> list[dict[str, object]]:
    """Read the persisted node documents out of a manifest document."""
    return cast("list[dict[str, object]]", manifest.to_document()["nodes"])


def _plan(*children: dict[str, object]) -> WorkPlan:
    return WorkPlan.from_mapping(_mapping(*children))


def _parse(*children: dict[str, object]) -> WorkPlan:
    return _plan(*children)


def _parse_workers(
    *children: dict[str, object], workers: tuple[dict[str, object], ...]
) -> WorkPlan:
    return WorkPlan.from_mapping({"workers": list(workers), "leaves": list(children)})


def _parse_sub_tls(*children: dict[str, object]) -> WorkPlan:
    return WorkPlan.from_mapping(
        {
            "leaves": list(children),
            "sub_tls": [{"name": "nested", "order": 1, "plan": {"leaves": []}}],
        }
    )


def _slice(
    slice_id: str,
    status: SliceStatus = SliceStatus.PENDING,
    *,
    depends_on: tuple[str, ...] = (),
) -> SliceState:
    return SliceState(
        id=slice_id,
        status=status,
        paths=(f"src/{slice_id}.py",),
        depends_on=depends_on,
        base_ref="main",
        test_plan=("just tl-loop-test",),
        agent_type=None,
        model=None,
        branch=None,
        worktree=None,
        pr_number=None,
        reviewed_head=None,
        attempts=0,
        verdict=None,
    )


def _record(
    slice_id: str,
    *,
    status: SliceStatus = SliceStatus.PENDING,
    depends_on: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": slice_id,
        "status": status.value,
        "paths": [f"src/{slice_id}.py"],
        "depends_on": depends_on or [],
        "base_ref": "main",
        "test_plan": ["just tl-loop-test"],
        "agent_type": None,
        "model": None,
        "branch": None,
        "worktree": None,
        "pr_number": None,
        "reviewed_head": None,
        "attempts": 0,
        "verdict": None,
    }


def _run_state(
    slices: dict[str, SliceState], tmp_path: Path, *, run_id: str = "gate"
) -> RunState:
    """Persist ``slices`` under a fresh run and return the state the gate reads."""
    store = RunStore(root_dir=tmp_path, run_id=run_id)
    create(run_id, {"slices": _encoded(slices)}, root_dir=store.root_dir)
    return store.load()


def _encoded(slices: Mapping[str, SliceState]) -> dict[str, dict[str, object]]:
    """Project typed slices back to the durable record shape a store accepts."""
    return {
        slice_id: {
            "id": current.id,
            "status": current.status.value,
            "paths": list(current.paths),
            "depends_on": list(current.depends_on),
            "base_ref": current.base_ref,
            "test_plan": list(current.test_plan),
            "agent_type": current.agent_type,
            "model": current.model,
            "branch": current.branch,
            "worktree": current.worktree,
            "pr_number": current.pr_number,
            "reviewed_head": current.reviewed_head,
            "attempts": current.attempts,
            "verdict": current.verdict.value if current.verdict else None,
        }
        for slice_id, current in slices.items()
    }


def _started(
    tmp_path: Path, run_id: str, plan_mapping: dict[str, object]
) -> tuple[WorkPlan, RunStore, "_Transport"]:
    """Persist a declared run whose every slice is still pending.

    Starting from the durable declaration rather than a live loop keeps these
    tests on the dispatch gate itself, instead of on review and merge
    choreography that would decide a leaf's status for unrelated reasons.
    """
    config = TLLoopConfig(root_dir=tmp_path)
    plan = WorkPlan.from_mapping(plan_mapping)
    manifest = build_plan_manifest(plan_mapping, scope_id=run_id)
    create(
        run_id,
        {
            "slices": dict(
                _bind_initial_slices(_initial_slices(plan, config), manifest)
            ),
            "plan_manifest": manifest.to_document(),
        },
        root_dir=tmp_path,
    )
    return plan, RunStore(root_dir=tmp_path, run_id=run_id), _Transport()


def _dispatch_pass(plan: WorkPlan, store: RunStore, transport: "_Transport") -> RunState:
    """Run one real dispatch pass against the persisted run."""
    config = TLLoopConfig(root_dir=store.root_dir)
    effects_log: list[EffectIntent] = []
    return _dispatch_children(
        plan,
        store.load(),
        config,
        EffectClient(transport),  # type: ignore[arg-type]
        effects_log,
        store,
    )


def _merge(store: RunStore, slice_id: str) -> None:
    """Record the authoritative merge of one slice in the durable checkpoint."""
    state = store.load()
    slices = {
        name: {
            **_encoded(state.slices)[name],
            "status": SliceStatus.MERGED.value if name == slice_id else current.status.value,
            "pr_number": 42 if name == slice_id else current.pr_number,
            "branch": f"main.{name}",
        }
        for name, current in state.slices.items()
    }
    store.checkpoint(
        driver._phase_from_state(state),
        slices,
        state.budgets,
        state.events.last_consumed_offset,
    )


def _policy() -> HarnessPolicy:
    role = {
        "allow": ["codex"],
        "cost_rank": {"codex": 1},
        "token_budget": 1000,
        "escalate_after_attempts": 1,
    }
    return validate_policy(
        {"roles": {"tl": dict(role), "worker": dict(role), "reviewer": dict(role)}}
    )


def _spawned(transport: "_Transport") -> list[str]:
    return [
        str(arguments.get("name")) for tool, arguments in transport.calls if tool == "spawn_leaf"
    ]


def _blocked_events(effects_log: list[EffectIntent]) -> list[Mapping[str, object]]:
    """Return the payloads of the durable events naming a blocked edge."""
    return [
        dict(entry.arguments)
        for entry in effects_log
        if entry.operation == "emit_controller_event"
        and "blocked_by" in entry.arguments
    ]


class _Transport:
    """A minimal transport that records spawns and accepts every other tool."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonObject]] = []

    def call_tool(
        self, role: str, name: str, tool_name: str, arguments: JsonObject
    ) -> JsonObject:
        del role, name
        self.calls.append((tool_name, arguments))
        if tool_name in {"spawn_leaf", "spawn_worker"}:
            return {"success": True, "result": {"agent_id": f"{tool_name}-agent"}}
        return {"success": True, "result": None}


def _chain_events(run_id: str, *names: str) -> list[EventEnvelope]:
    events: list[EventEnvelope] = []
    seq = 1
    for name in names:
        events.append(_event(seq, "child_spawned", name, run_id=run_id))
        seq += 1
        events.append(_event(seq, "child_completed", name, run_id=run_id))
        seq += 1
    events.append(_event(seq, "all_children_done", run_id=run_id))
    return events

