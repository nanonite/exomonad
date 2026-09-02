"""Immutable recursive plan manifests and revision safety checks."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from .serialization import dumps

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_NODE_KINDS = frozenset({"worker", "leaf", "sub_tl", "legacy"})
MANIFEST_ROLES = frozenset({"root", "non_root"})
MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "manifest_revision",
        "scope_id",
        "parent_scope_id",
        "role",
        "owned_branch",
        "parent_integration_target",
        "source_order",
        "nodes",
        "ordered_stages",
        "child_manifest_digests",
        "child_manifests",
        "digest",
    }
)
MANIFEST_NODE_KEYS = frozenset(
    {
        "node_id",
        "parent_id",
        "kind",
        "name",
        "source_index",
        "order",
        "task",
        "agent_type",
        "boundary",
        "owned_branch",
        "parent_integration_target",
        "worktree",
        "declaration",
        "integration_contract",
        "child_manifest_digest",
    }
)


class ManifestError(ValueError):
    """A plan manifest is malformed or violates its immutable contract."""


@dataclass(frozen=True)
class ManifestNode:
    """One direct child declaration in a recursive scope."""

    node_id: str
    parent_id: str
    kind: str
    name: str
    source_index: int
    order: int | None
    task: str
    agent_type: str | None
    boundary: tuple[str, ...]
    owned_branch: str
    parent_integration_target: str | None
    worktree: str | None
    declaration: Mapping[str, object] = field(default_factory=dict)
    integration_contract: Mapping[str, object] = field(default_factory=dict)
    child_manifest_digest: str | None = None

    def __post_init__(self) -> None:
        for name in ("node_id", "parent_id", "name", "task", "owned_branch"):
            _require_text(getattr(self, name), f"manifest node {name}")
        if self.kind not in MANIFEST_NODE_KINDS:
            raise ManifestError(f"manifest node kind {self.kind!r} is not recognized")
        if type(self.source_index) is not int or self.source_index < 0:
            raise ManifestError("manifest node source_index must be non-negative")
        if self.order is not None and (type(self.order) is not int or self.order <= 0):
            raise ManifestError("manifest node order must be positive or null")
        if self.kind == "sub_tl" and self.order is None:
            raise ManifestError("sub-TL manifest nodes require an order")
        if self.kind != "sub_tl" and self.order is not None:
            raise ManifestError("only sub-TL manifest nodes may carry an order")
        if self.agent_type is not None:
            _require_text(self.agent_type, "manifest node agent_type")
        if self.parent_integration_target is not None:
            _require_text(self.parent_integration_target, "manifest node parent target")
        if self.worktree is not None:
            _require_text(self.worktree, "manifest node worktree")
        declaration = _json_mapping(self.declaration, "manifest node declaration")
        if any(not isinstance(path, str) or not path for path in self.boundary):
            raise ManifestError("manifest node boundary must contain non-empty strings")
        if len(set(self.boundary)) != len(self.boundary):
            raise ManifestError("manifest node boundary must be unique")
        contract = _json_mapping(self.integration_contract, "manifest integration contract")
        object.__setattr__(self, "declaration", _freeze_mapping(declaration))
        object.__setattr__(self, "integration_contract", _freeze_mapping(contract))
        if self.child_manifest_digest is not None:
            _require_text(self.child_manifest_digest, "child manifest digest")

    def identity(self) -> tuple[object, ...]:
        """Return fields that cannot change after dispatch or completion."""
        return (
            self.node_id,
            self.parent_id,
            self.kind,
            self.name,
            self.source_index,
            self.order,
            self.task,
            self.agent_type,
            self.boundary,
            self.owned_branch,
            self.parent_integration_target,
            self.worktree,
            tuple(sorted(self.declaration.items())),
            tuple(sorted(self.integration_contract.items())),
            self.child_manifest_digest,
        )

    def to_document(self) -> dict[str, object]:
        """Serialize the node with deterministic list representations."""
        return {
            "node_id": self.node_id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "name": self.name,
            "source_index": self.source_index,
            "order": self.order,
            "task": self.task,
            "agent_type": self.agent_type,
            "boundary": list(self.boundary),
            "owned_branch": self.owned_branch,
            "parent_integration_target": self.parent_integration_target,
            "worktree": self.worktree,
            "declaration": _thaw_json(self.declaration),
            "integration_contract": _thaw_json(self.integration_contract),
            "child_manifest_digest": self.child_manifest_digest,
        }

    @classmethod
    def from_document(cls, value: Mapping[str, object]) -> ManifestNode:
        """Decode one closed manifest node."""
        if not isinstance(value, Mapping):
            raise ManifestError("manifest node must be an object")
        unknown = sorted(set(value) - MANIFEST_NODE_KEYS)
        if unknown:
            raise ManifestError(f"manifest node contains unknown keys: {', '.join(unknown)}")
        boundary = value.get("boundary", ())
        if not isinstance(boundary, Sequence) or isinstance(boundary, (str, bytes)):
            raise ManifestError("manifest node boundary must be an array")
        contract = value.get("integration_contract", {})
        if not isinstance(contract, Mapping):
            raise ManifestError("manifest node integration_contract must be an object")
        declaration = value.get("declaration", {})
        if not isinstance(declaration, Mapping):
            raise ManifestError("manifest node declaration must be an object")
        return cls(
            node_id=_required_value(value, "node_id"),
            parent_id=_required_value(value, "parent_id"),
            kind=_required_value(value, "kind"),
            name=_required_value(value, "name"),
            source_index=_required_int(value, "source_index"),
            order=_optional_int(value.get("order"), "order"),
            task=_required_value(value, "task"),
            agent_type=_optional_text(value.get("agent_type"), "agent_type"),
            boundary=tuple(_require_text(item, "boundary item") for item in boundary),
            owned_branch=_required_value(value, "owned_branch"),
            parent_integration_target=_optional_text(
                value.get("parent_integration_target"), "parent_integration_target"
            ),
            worktree=_optional_text(value.get("worktree"), "worktree"),
            declaration=declaration,
            integration_contract=contract,
            child_manifest_digest=_optional_text(
                value.get("child_manifest_digest"), "child_manifest_digest"
            ),
        )


@dataclass(frozen=True)
class PlanManifest:
    """The immutable recursive declaration authoritative for one TL scope."""

    scope_id: str
    parent_scope_id: str | None
    role: str
    owned_branch: str
    parent_integration_target: str | None
    source_order: tuple[str, ...]
    nodes: tuple[ManifestNode, ...]
    ordered_stages: tuple[tuple[int, tuple[str, ...]], ...] = ()
    child_manifest_digests: Mapping[str, str] = field(default_factory=dict)
    child_manifests: Mapping[str, PlanManifest] = field(default_factory=dict)
    schema_version: int = MANIFEST_SCHEMA_VERSION
    manifest_revision: int = 1
    digest: str | None = None

    def __post_init__(self) -> None:
        for name in ("scope_id", "owned_branch"):
            _require_text(getattr(self, name), f"manifest {name}")
        if self.parent_scope_id is not None:
            _require_text(self.parent_scope_id, "manifest parent_scope_id")
        if self.role not in MANIFEST_ROLES:
            raise ManifestError(f"manifest role {self.role!r} is not recognized")
        if type(self.schema_version) is not int or self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ManifestError("manifest schema version is unsupported")
        if type(self.manifest_revision) is not int or self.manifest_revision < 1:
            raise ManifestError("manifest revision must be positive")
        if self.parent_integration_target is not None:
            _require_text(self.parent_integration_target, "manifest parent target")
        nodes = tuple(self.nodes)
        if any(not isinstance(node, ManifestNode) for node in nodes):
            raise TypeError("manifest nodes must be ManifestNode values")
        ids = tuple(node.node_id for node in nodes)
        if len(set(ids)) != len(ids):
            raise ManifestError("manifest node IDs must be unique")
        names = tuple(node.name for node in nodes)
        if len(set(names)) != len(names):
            raise ManifestError("manifest node names must be unique")
        if tuple(node.source_index for node in nodes) != tuple(range(len(nodes))):
            raise ManifestError("manifest source indexes must be contiguous")
        if any(node.parent_id != self.scope_id for node in nodes):
            raise ManifestError("manifest node parent does not match scope")
        if tuple(self.source_order) != ids:
            raise ManifestError("manifest source_order must match node source order")
        stages = _normalize_stages(self.ordered_stages, ids, nodes)
        child_digests = _json_mapping(self.child_manifest_digests, "child manifest digests")
        raw_children = self.child_manifests
        if not isinstance(raw_children, Mapping):
            raise ManifestError("child manifests must be an object")
        children: dict[str, PlanManifest] = {}
        for node_id, child in raw_children.items():
            _require_text(node_id, "child manifest node")
            if not isinstance(child, PlanManifest):
                raise ManifestError("child manifests must contain PlanManifest values")
            if child.scope_id != node_id or child.parent_scope_id != self.scope_id:
                raise ManifestError("child manifest scope does not match its parent node")
            children[node_id] = child
        for node_id, digest in child_digests.items():
            if node_id not in ids:
                raise ManifestError("child manifest digest names an unknown node")
            _require_text(digest, "child manifest digest")
        for node in nodes:
            expected = child_digests.get(node.node_id)
            if node.kind == "sub_tl" and expected != node.child_manifest_digest:
                raise ManifestError("sub-TL child digest is not correlated with its node")
            if node.kind == "sub_tl" and node.node_id not in children:
                raise ManifestError("sub-TL node is missing its recursive child manifest")
            if node.kind != "sub_tl" and expected is not None:
                raise ManifestError("only sub-TL nodes may have child manifests")
            if node.kind != "sub_tl" and node.node_id in children:
                raise ManifestError("only sub-TL nodes may have child manifests")
            child = children.get(node.node_id)
            if child is not None and child.digest != expected:
                raise ManifestError("child manifest digest does not match its persisted document")
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "source_order", ids)
        object.__setattr__(self, "ordered_stages", stages)
        object.__setattr__(self, "child_manifest_digests", MappingProxyType(child_digests))
        object.__setattr__(self, "child_manifests", MappingProxyType(children))
        computed = _digest(self._payload())
        if self.digest is not None and self.digest != computed:
            raise ManifestError("manifest digest does not match canonical content")
        object.__setattr__(self, "digest", computed)

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "manifest_revision": self.manifest_revision,
            "scope_id": self.scope_id,
            "parent_scope_id": self.parent_scope_id,
            "role": self.role,
            "owned_branch": self.owned_branch,
            "parent_integration_target": self.parent_integration_target,
            "source_order": list(self.source_order),
            "nodes": [node.to_document() for node in self.nodes],
            "ordered_stages": [
                {"order": order, "nodes": list(node_ids)} for order, node_ids in self.ordered_stages
            ],
            "child_manifest_digests": dict(self.child_manifest_digests),
            "child_manifests": {
                node_id: child.to_document() for node_id, child in self.child_manifests.items()
            },
        }

    def to_document(self) -> dict[str, object]:
        """Return the complete durable manifest, including its digest."""
        return {**self._payload(), "digest": self.digest}

    @classmethod
    def from_document(cls, value: Mapping[str, object]) -> PlanManifest:
        """Decode and verify one manifest document."""
        if not isinstance(value, Mapping):
            raise ManifestError("manifest must be an object")
        unknown = sorted(set(value) - MANIFEST_KEYS)
        if unknown:
            raise ManifestError(f"manifest contains unknown keys: {', '.join(unknown)}")
        nodes = value.get("nodes")
        if not isinstance(nodes, list):
            raise ManifestError("manifest nodes must be an array")
        stages = value.get("ordered_stages", [])
        if not isinstance(stages, list):
            raise ManifestError("manifest ordered_stages must be an array")
        parsed_stages: list[tuple[int, tuple[str, ...]]] = []
        for stage in stages:
            if not isinstance(stage, Mapping):
                raise ManifestError("manifest stage must be an object")
            raw_nodes = stage.get("nodes")
            if not isinstance(raw_nodes, list):
                raise ManifestError("manifest stage nodes must be an array")
            parsed_stages.append(
                (
                    _required_int(stage, "order"),
                    tuple(_require_text(item, "stage node") for item in raw_nodes),
                )
            )
        raw_digests = value.get("child_manifest_digests", {})
        if not isinstance(raw_digests, Mapping):
            raise ManifestError("manifest child_manifest_digests must be an object")
        source_order = value.get("source_order")
        if not isinstance(source_order, list):
            raise ManifestError("manifest source_order must be an array")
        raw_children = value.get("child_manifests", {})
        if not isinstance(raw_children, Mapping):
            raise ManifestError("manifest child_manifests must be an object")
        children = {
            _require_text(node_id, "child manifest node"): cls.from_document(child)
            for node_id, child in raw_children.items()
        }
        return cls(
            scope_id=_required_value(value, "scope_id"),
            parent_scope_id=_optional_text(value.get("parent_scope_id"), "parent_scope_id"),
            role=_required_value(value, "role"),
            owned_branch=_required_value(value, "owned_branch"),
            parent_integration_target=_optional_text(
                value.get("parent_integration_target"), "parent_integration_target"
            ),
            source_order=tuple(_require_text(item, "source node") for item in source_order),
            nodes=tuple(ManifestNode.from_document(node) for node in nodes),
            ordered_stages=tuple(parsed_stages),
            child_manifest_digests={
                _require_text(key, "child node"): _require_text(value, "child digest")
                for key, value in raw_digests.items()
            },
            child_manifests=children,
            schema_version=_required_int(value, "schema_version"),
            manifest_revision=_required_int(value, "manifest_revision"),
            digest=_optional_text(value.get("digest"), "digest"),
        )

    def node(self, node_id: str) -> ManifestNode:
        """Resolve a direct node or fail closed."""
        for candidate in self.nodes:
            if candidate.node_id == node_id:
                return candidate
        raise ManifestError(f"manifest node {node_id!r} does not exist")


def build_plan_manifest(
    plan: Mapping[str, object],
    *,
    scope_id: str,
    parent_scope_id: str | None = None,
    role: str = "root",
    owned_branch: str = "main",
    parent_integration_target: str | None = None,
    manifest_revision: int = 1,
) -> PlanManifest:
    """Build a canonical manifest from one validated recursive plan mapping."""
    if not isinstance(plan, Mapping):
        raise ManifestError("plan must be an object")
    unknown = sorted(set(plan) - {"workers", "leaves", "sub_tls"})
    if unknown:
        raise ManifestError(f"plan contains unknown keys: {', '.join(unknown)}")
    nodes: list[ManifestNode] = []
    child_digests: dict[str, str] = {}
    child_manifests: dict[str, PlanManifest] = {}
    source_index = 0
    for kind, key in (("worker", "workers"), ("leaf", "leaves"), ("sub_tl", "sub_tls")):
        entries = plan.get(key, [])
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            raise ManifestError(f"plan {key} must be an array")
        for raw in entries:
            if not isinstance(raw, Mapping):
                raise ManifestError(f"plan {key} entries must be objects")
            name = _required_value(raw, "name")
            node_id = f"{scope_id}/{kind}/{name}"
            task = _required_value(raw, "task") if kind != "sub_tl" else f"sub-TL {name}"
            order = raw.get("order") if kind == "sub_tl" else None
            if kind == "sub_tl" and type(order) is not int:
                raise ManifestError(f"sub-TL {name!r} requires an integer order")
            boundary = raw.get("boundary", ()) if kind == "leaf" else ()
            if not isinstance(boundary, Sequence) or isinstance(boundary, (str, bytes)):
                raise ManifestError(f"manifest boundary for {name!r} must be an array")
            branch = str(raw.get("branch") or f"{owned_branch}.{name}")
            worktree = raw.get("worktree")
            child_digest: str | None = None
            if kind == "sub_tl":
                nested = raw.get("plan", {})
                if not isinstance(nested, Mapping):
                    raise ManifestError(f"sub-TL {name!r} plan must be an object")
                child = build_plan_manifest(
                    nested,
                    scope_id=node_id,
                    parent_scope_id=scope_id,
                    role="non_root",
                    owned_branch=branch,
                    parent_integration_target=owned_branch,
                    # Child scopes have their own revision stream.  Revising a
                    # parent declaration must not manufacture a new digest for
                    # an unchanged nested plan.
                    manifest_revision=1,
                )
                child_digest = child.digest
            node = ManifestNode(
                node_id=node_id,
                parent_id=scope_id,
                kind=kind,
                name=name,
                source_index=source_index,
                order=order,
                task=task,
                agent_type=_optional_text(raw.get("agent_type"), "agent_type"),
                boundary=tuple(_require_text(item, "boundary item") for item in boundary),
                owned_branch=branch,
                parent_integration_target=owned_branch,
                worktree=_optional_text(worktree, "worktree"),
                declaration=_node_declaration(raw, kind),
                integration_contract=_integration_contract(raw.get("integration")),
                child_manifest_digest=child_digest,
            )
            nodes.append(node)
            if child_digest is not None:
                child_digests[node_id] = child_digest
                child_manifests[node_id] = child
            source_index += 1
    ordered = _ordered_from_nodes(nodes)
    return PlanManifest(
        scope_id=scope_id,
        parent_scope_id=parent_scope_id,
        role=role,
        owned_branch=owned_branch,
        parent_integration_target=parent_integration_target,
        source_order=tuple(node.node_id for node in nodes),
        nodes=tuple(nodes),
        ordered_stages=ordered,
        child_manifest_digests=child_digests,
        child_manifests=child_manifests,
        manifest_revision=manifest_revision,
    )


def build_legacy_manifest(document: Mapping[str, object], *, run_id: str) -> PlanManifest:
    """Create a deterministic, explicitly legacy manifest for migration."""
    raw_slices = document.get("slices", {})
    if not isinstance(raw_slices, Mapping):
        raise ManifestError("legacy slices must be an object")
    _reject_ambiguous_nested_slices(raw_slices)
    stages = document.get("ordered_stages", [])
    normalized_stages = _legacy_stages(stages)
    ordered_sequence = tuple(name for stage in normalized_stages for name in stage["sub_tls"])
    if len(set(ordered_sequence)) != len(ordered_sequence):
        raise ManifestError("legacy ordered stages repeat a sub-TL name")
    ordered_names = set(ordered_sequence)
    if not ordered_names.issubset(raw_slices):
        missing = sorted(ordered_names - set(raw_slices))
        raise ManifestError(
            "legacy ordered stage names are absent from slices: "
            + ", ".join(repr(name) for name in missing)
        )
    plan = {
        "workers": [
            {"name": name, "task": "legacy", "order": None}
            for name in raw_slices
            if name not in ordered_names
        ],
        "leaves": [],
        "sub_tls": [
            {"name": name, "order": _legacy_order(stages, name), "plan": {}}
            for name in ordered_sequence
        ],
    }
    # Legacy kind is retained in the node declaration to make the ambiguity
    # visible to operators; it is never treated as a new worker authorization.
    manifest = build_plan_manifest(plan, scope_id=run_id, owned_branch="legacy")
    legacy_nodes = tuple(
        ManifestNode(
            **{
                **node.to_document(),
                "kind": "sub_tl" if node.name in ordered_names else "legacy",
                "order": node.order if node.name in ordered_names else None,
                "child_manifest_digest": (
                    node.child_manifest_digest if node.name in ordered_names else None
                ),
            }
        )
        for node in manifest.nodes
    )
    return PlanManifest(
        scope_id=manifest.scope_id,
        parent_scope_id=None,
        role="root",
        owned_branch="legacy",
        parent_integration_target=None,
        source_order=tuple(node.node_id for node in legacy_nodes),
        nodes=legacy_nodes,
        ordered_stages=manifest.ordered_stages,
        child_manifest_digests={
            node_id: digest
            for node_id, digest in manifest.child_manifest_digests.items()
            if node_id.rsplit("/", 1)[-1] in ordered_names
        },
        child_manifests={
            node_id: child
            for node_id, child in manifest.child_manifests.items()
            if node_id.rsplit("/", 1)[-1] in ordered_names
        },
        manifest_revision=1,
    )


def validate_manifest_revision(
    previous: PlanManifest,
    candidate: PlanManifest,
    *,
    protected_node_ids: set[str] | frozenset[str],
) -> None:
    """Allow only monotonic revisions with immutable dispatched history."""
    if candidate.manifest_revision <= previous.manifest_revision:
        raise ManifestError("manifest revision must increase monotonically")
    if (
        candidate.scope_id != previous.scope_id
        or candidate.parent_scope_id != previous.parent_scope_id
        or candidate.role != previous.role
        or candidate.owned_branch != previous.owned_branch
        or candidate.parent_integration_target != previous.parent_integration_target
    ):
        raise ManifestError("manifest revision cannot change scope ownership")
    previous_nodes = {node.node_id: node for node in previous.nodes}
    candidate_nodes = {node.node_id: node for node in candidate.nodes}
    removed = sorted(set(previous_nodes) - set(candidate_nodes))
    if removed:
        raise ManifestError(
            "manifest revisions are additive-only; cannot remove nodes: "
            + ", ".join(repr(node_id) for node_id in removed)
        )
    for node_id in protected_node_ids:
        old = previous_nodes.get(node_id)
        new = candidate_nodes.get(node_id)
        if old is None or new is None:
            raise ManifestError(f"manifest revision cannot remove protected node {node_id!r}")
        if old.identity() != new.identity():
            raise ManifestError(f"manifest revision mutates protected node {node_id!r}")
    for node_id, old in previous_nodes.items():
        new = candidate_nodes.get(node_id)
        if new is not None and old.identity() != new.identity() and node_id in protected_node_ids:
            raise ManifestError(f"manifest revision mutates protected node {node_id!r}")


def _normalize_stages(
    stages: tuple[tuple[int, tuple[str, ...]], ...],
    node_ids: tuple[str, ...],
    nodes: tuple[ManifestNode, ...],
) -> tuple[tuple[int, tuple[str, ...]], ...]:
    expected = 1
    known = set(node_ids)
    by_id = {node.node_id: node for node in nodes}
    normalized: list[tuple[int, tuple[str, ...]]] = []
    for order, raw_ids in stages:
        if type(order) is not int or order != expected:
            raise ManifestError("manifest stages must be contiguous from order one")
        child_ids = tuple(raw_ids)
        if not child_ids or len(set(child_ids)) != len(child_ids):
            raise ManifestError("manifest stages must contain unique nodes")
        if any(node_id not in known or by_id[node_id].kind != "sub_tl" for node_id in child_ids):
            raise ManifestError("manifest stage contains a non-sub-TL or unknown node")
        normalized.append((order, child_ids))
        expected += 1
    if {node_id for _, ids in normalized for node_id in ids} != {
        node.node_id for node in nodes if node.kind == "sub_tl"
    }:
        raise ManifestError("every sub-TL must belong to exactly one manifest stage")
    return tuple(normalized)


def _ordered_from_nodes(nodes: Sequence[ManifestNode]) -> tuple[tuple[int, tuple[str, ...]], ...]:
    groups: dict[int, list[str]] = {}
    for node in nodes:
        if node.kind == "sub_tl":
            assert node.order is not None
            groups.setdefault(node.order, []).append(node.node_id)
    return tuple((order, tuple(groups[order])) for order in sorted(groups))


def _integration_contract(value: object) -> Mapping[str, object]:
    if value is None:
        return {
            "aggregate_pr_required": True,
            "base_revalidation_required": True,
            "leaf_review_owner": "leaf",
            "aggregate_review_owner": "aggregate",
            "aggregate_repair_owner": "aggregate",
            "merge_strategy": "merge",
        }
    if not isinstance(value, Mapping):
        raise ManifestError("integration contract must be an object")
    return _json_mapping(value, "integration contract")


def _node_declaration(value: Mapping[str, object], kind: str) -> Mapping[str, object]:
    """Retain task metadata needed to reconstruct the exact child declaration."""
    fields = {
        "worker": ("task_timeout_seconds",),
        "leaf": (
            "context",
            "read_first",
            "steps",
            "verify",
            "done_criteria",
            "task_timeout_seconds",
        ),
        "sub_tl": ("order_explicit", "task_timeout_seconds"),
        "legacy": (),
    }[kind]
    declaration = {name: value[name] for name in fields if name in value}
    return _json_mapping(declaration, "manifest node declaration")


def _json_mapping(value: Mapping[str, object], field_name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{field_name} must be an object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ManifestError(f"{field_name} keys must be non-empty strings")
        try:
            dumps(item, sort_keys=True)
            result[key] = _freeze_json(item, field_name)
        except (TypeError, ValueError, ManifestError) as error:
            raise ManifestError(f"{field_name} contains non-JSON value {key!r}") from error
    return result


def _freeze_mapping(value: Mapping[str, object]) -> MappingProxyType:
    return MappingProxyType(
        {key: _freeze_json(item, "manifest mapping") for key, item in value.items()}
    )


def _freeze_json(value: object, field_name: str) -> object:
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ManifestError(f"{field_name} keys must be non-empty strings")
            frozen[key] = _freeze_json(item, field_name)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, field_name) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _digest(payload: Mapping[str, object]) -> str:
    encoded = dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _legacy_order(stages: object, name: str) -> int:
    for stage in _legacy_stages(stages):
        if name in stage["sub_tls"]:
            value = stage.get("order")
            if type(value) is int and value > 0:
                return value
    return 1


def _legacy_stages(stages: object) -> tuple[dict[str, object], ...]:
    if not isinstance(stages, list):
        raise ManifestError("legacy ordered_stages must be an array")
    normalized: list[dict[str, object]] = []
    for index, value in enumerate(stages):
        if not isinstance(value, Mapping):
            raise ManifestError(f"legacy ordered_stages[{index}] must be an object")
        members = value.get("sub_tls", [])
        if not isinstance(members, list) or any(
            not isinstance(name, str) or not name for name in members
        ):
            raise ManifestError(f"legacy ordered_stages[{index}].sub_tls must be an array of names")
        normalized.append({**value, "sub_tls": list(members)})
    return tuple(normalized)


def _reject_ambiguous_nested_slices(raw_slices: Mapping[object, object]) -> None:
    nested_keys = frozenset(
        {"children", "sub_tls", "child_scopes", "parent_scope_id", "scope_path"}
    )
    for slice_id, raw in raw_slices.items():
        if not isinstance(raw, Mapping):
            continue
        present = sorted(nested_keys.intersection(raw))
        if present:
            raise ManifestError(
                f"legacy nested scope for {slice_id!r} cannot be reconstructed from "
                + ", ".join(present)
            )


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{field_name} must be a non-empty string")
    return value


def _required_value(value: Mapping[str, object], key: str) -> str:
    return _require_text(value.get(key), f"manifest {key}")


def _required_int(value: Mapping[str, object], key: str) -> int:
    raw = value.get(key)
    if type(raw) is not int:
        raise ManifestError(f"manifest {key} must be an integer")
    return raw


def _optional_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ManifestError(f"manifest {field_name} must be an integer or null")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, f"manifest {field_name}")


__all__ = [
    "MANIFEST_NODE_KINDS",
    "MANIFEST_ROLES",
    "MANIFEST_SCHEMA_VERSION",
    "ManifestError",
    "ManifestNode",
    "PlanManifest",
    "build_legacy_manifest",
    "build_plan_manifest",
    "validate_manifest_revision",
]
