"""Evidence-bound replacement of the pre-manifest continuation placeholder."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from .plan_manifest import ManifestNode, PlanManifest
from .schema import RunState, SliceState, SliceStatus


class JournalSnapshot(Protocol):
    """Small read-only journal interface required during migration."""

    def snapshot(self) -> tuple[dict[str, object], ...]: ...


class LegacyManifestDisposition(str, Enum):
    """Outcome of proving one legacy scope against a canonical candidate."""

    PROVEN = "proven"
    AMBIGUOUS = "ambiguous"
    MISSING_EVIDENCE = "missing_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"


@dataclass(frozen=True)
class LegacyNodeProof:
    """Auditable proof and durable binding for one legacy node."""

    old_node_id: str
    new_node_id: str | None
    slice_id: str
    disposition: LegacyManifestDisposition
    evidence: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    branch: str | None = None
    worktree: str | None = None

    @property
    def reason(self) -> str:
        return "; ".join(self.conflicts or self.missing or self.evidence) or "no proof recorded"

    def to_document(self) -> dict[str, object]:
        """Return a bounded, operator-safe migration record."""
        return {
            "old_node_id": self.old_node_id,
            "new_node_id": self.new_node_id,
            "slice_id": self.slice_id,
            "disposition": self.disposition.value,
            "evidence": list(self.evidence),
            "missing": list(self.missing),
            "conflicts": list(self.conflicts),
            "branch": self.branch,
            "worktree": self.worktree,
        }


@dataclass(frozen=True)
class LegacyManifestReconciliation:
    """Complete, deterministic result of an active legacy migration attempt."""

    disposition: LegacyManifestDisposition
    bindings: Mapping[str, str]
    proofs: tuple[LegacyNodeProof, ...]
    reason: str

    @property
    def proven(self) -> bool:
        return self.disposition is LegacyManifestDisposition.PROVEN

    def gate_name(self) -> str:
        """Return a stable gate name containing the actionable failure."""
        return f"plan-manifest-migration:{self.disposition.value}:{self.reason}"

    def to_document(self) -> dict[str, object]:
        return {
            "disposition": self.disposition.value,
            "bindings": dict(self.bindings),
            "proofs": [proof.to_document() for proof in self.proofs],
            "reason": self.reason,
        }


def reconcile_legacy_manifest(
    previous: PlanManifest,
    candidate: PlanManifest,
    state: RunState,
    journal: JournalSnapshot,
    *,
    child_checkpoint_root: Path | None = None,
) -> LegacyManifestReconciliation:
    """Prove every active legacy binding before replacing the placeholder."""
    if not _is_legacy_root(previous):
        return _failure((), "previous manifest is not an active legacy root", ambiguous=True)
    if previous.scope_id != candidate.scope_id or candidate.role != previous.role:
        return _failure((), "candidate changes legacy scope identity", conflict=True)
    old_nodes = {node.name: node for node in previous.nodes}
    candidate_nodes = {node.name: node for node in candidate.nodes}
    if len(old_nodes) != len(previous.nodes) or len(candidate_nodes) != len(candidate.nodes):
        return _failure((), "duplicate legacy or candidate node names", ambiguous=True)
    extra_names = sorted(set(candidate_nodes) - set(old_nodes))
    if extra_names:
        proofs = tuple(
            LegacyNodeProof(
                old_node_id=f"{previous.scope_id}/unknown/{name}",
                new_node_id=candidate_nodes[name].node_id,
                slice_id=name,
                disposition=LegacyManifestDisposition.CONFLICTING_EVIDENCE,
                conflicts=("candidate adds undeclared active node",),
            )
            for name in extra_names
        )
        return LegacyManifestReconciliation(
            disposition=LegacyManifestDisposition.CONFLICTING_EVIDENCE,
            bindings={},
            proofs=proofs,
            reason="candidate adds undeclared active node(s): " + ", ".join(extra_names),
        )
    entries = journal.snapshot() if hasattr(journal, "snapshot") else tuple(journal)
    proofs: list[LegacyNodeProof] = []
    bindings: dict[str, str] = {}
    for old in previous.nodes:
        current = state.slices.get(old.name)
        new = candidate_nodes.get(old.name)
        proof = _prove_node(
            old,
            new,
            current,
            entries,
            candidate,
            child_checkpoint_root,
        )
        proofs.append(proof)
        if new is not None:
            bindings[old.node_id] = new.node_id
    for slice_id in state.slices:
        if slice_id not in old_nodes:
            proofs.append(
                LegacyNodeProof(
                    slice_id=slice_id,
                    old_node_id=f"{previous.scope_id}/unknown/{slice_id}",
                    new_node_id=None,
                    disposition=LegacyManifestDisposition.CONFLICTING_EVIDENCE,
                    conflicts=("slice is not declared by the legacy manifest",),
                )
            )
    disposition = _overall_disposition(proofs)
    reason = "; ".join(
        f"{proof.slice_id}: {proof.reason}"
        for proof in proofs
        if proof.disposition is not LegacyManifestDisposition.PROVEN
    )
    return LegacyManifestReconciliation(
        disposition=disposition,
        bindings=bindings,
        proofs=tuple(proofs),
        reason=reason or "all legacy bindings have authoritative evidence",
    )


def _prove_node(
    old: ManifestNode,
    new: ManifestNode | None,
    current: SliceState | None,
    entries: Sequence[Mapping[str, object]],
    candidate: PlanManifest,
    child_checkpoint_root: Path | None,
) -> LegacyNodeProof:
    if new is None:
        return _proof(
            old, None, LegacyManifestDisposition.MISSING_EVIDENCE, missing=("candidate node",)
        )
    if current is None:
        return _proof(
            old, new, LegacyManifestDisposition.MISSING_EVIDENCE, missing=("slice state",)
        )
    if current.id != old.name or current.manifest_node_id != old.node_id:
        return _proof(
            old,
            new,
            LegacyManifestDisposition.CONFLICTING_EVIDENCE,
            conflicts=("scope/name binding",),
        )
    if current.status in {SliceStatus.PENDING, SliceStatus.READY}:
        if any(
            (
                current.dispatch_intent_id,
                current.dispatch_agent_id,
                current.pr_number,
                current.action,
            )
        ):
            return _proof(
                old,
                new,
                LegacyManifestDisposition.CONFLICTING_EVIDENCE,
                conflicts=("undispatched state contains runtime evidence",),
            )
        return _proof(
            old,
            new,
            LegacyManifestDisposition.PROVEN,
            evidence=("exact scope/name and undispatched state",),
        )
    if not current.dispatch_agent_id or not _invocation_id(current):
        return _proof(
            old,
            new,
            LegacyManifestDisposition.MISSING_EVIDENCE,
            missing=("dispatch agent and invocation",),
        )
    if new.kind in {"worker", "leaf"}:
        spawn = _prove_spawn(old, new, current, entries)
    elif new.kind == "sub_tl":
        spawn = _prove_child(new, current, candidate, child_checkpoint_root)
    else:
        return _proof(
            old,
            new,
            LegacyManifestDisposition.CONFLICTING_EVIDENCE,
            conflicts=(f"candidate kind {new.kind!r} is not resumable",),
        )
    if spawn[0] is not LegacyManifestDisposition.PROVEN:
        return _proof(
            old,
            new,
            spawn[0],
            evidence=spawn[1],
            missing=spawn[2],
            conflicts=spawn[3],
            branch=spawn[4],
            worktree=spawn[5],
        )
    identity = _prove_publication_and_review(old, new, current, spawn[4])
    if identity[0] is not LegacyManifestDisposition.PROVEN:
        return _proof(
            old,
            new,
            identity[0],
            evidence=spawn[1] + identity[1],
            missing=identity[2],
            conflicts=identity[3],
            branch=spawn[4],
            worktree=spawn[5],
        )
    action = _prove_action(current, entries)
    if action[0] is not LegacyManifestDisposition.PROVEN:
        return _proof(
            old,
            new,
            action[0],
            evidence=spawn[1] + identity[1],
            missing=action[2],
            conflicts=action[3],
            branch=spawn[4],
            worktree=spawn[5],
        )
    return _proof(
        old,
        new,
        LegacyManifestDisposition.PROVEN,
        evidence=spawn[1] + identity[1] + action[1],
        branch=spawn[4],
        worktree=spawn[5],
    )


def _prove_spawn(
    old: ManifestNode,
    new: ManifestNode,
    current: SliceState,
    entries: Sequence[Mapping[str, object]],
) -> tuple[
    LegacyManifestDisposition,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    str | None,
    str | None,
]:
    operation = "spawn_leaf" if new.kind == "leaf" else "spawn_worker"
    matches = [
        entry
        for entry in entries
        if entry.get("operation") == operation and entry.get("target") == current.id
    ]
    correlated = [
        entry
        for entry in matches
        if _mapping(entry.get("arguments")).get("intent_id") == current.dispatch_intent_id
    ]
    if not correlated:
        return _missing("confirmed spawn operation")
    if len(correlated) != 1:
        return _conflict("duplicate confirmed spawn operations")
    entry = correlated[0]
    args = _mapping(entry.get("arguments"))
    if entry.get("status") != "confirmed":
        return _missing("confirmed spawn result")
    required = {"name": new.name, "task": new.task}
    if new.kind == "leaf":
        required["boundary"] = list(new.boundary)
    conflicts = tuple(
        key for key, expected in required.items() if not _same(args.get(key), expected)
    )
    expected_agent = new.agent_type or current.agent_type
    if not isinstance(args.get("agent_type"), str) or (
        expected_agent is not None and args.get("agent_type") != expected_agent
    ):
        conflicts += ("agent_type",)
    intent_id = current.dispatch_intent_id
    if not intent_id or args.get("intent_id") != intent_id:
        conflicts += ("dispatch intent",)
    branch = _nested_text(entry, "branch_name") or _nested_text(entry, "branch")
    worktree = _nested_text(entry, "worktree_path") or _nested_text(entry, "worktree")
    if not branch or not _branch_matches(branch, current.branch, new.owned_branch):
        conflicts += ("owned branch",)
    if not worktree or current.worktree and current.worktree != worktree:
        conflicts += ("worktree",)
    conflicts += _declaration_conflicts(new, current, args)
    if conflicts:
        return _conflict(*conflicts)
    evidence = (
        "confirmed spawn",
        "complete declaration",
        "dispatch intent",
        "branch/worktree result",
    )
    return LegacyManifestDisposition.PROVEN, evidence, (), (), branch, worktree


def _prove_child(
    new: ManifestNode,
    current: SliceState,
    candidate: PlanManifest,
    root: Path | None,
) -> tuple[
    LegacyManifestDisposition,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    str | None,
    str | None,
]:
    if root is None:
        return _missing("nested child checkpoint")
    path = root / current.id / "run.json"
    if not path.exists():
        return _missing("nested child checkpoint")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        child = PlanManifest.from_document(document.get("plan_manifest"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        return _conflict(f"invalid nested child checkpoint: {error}")
    if (
        child.digest != new.child_manifest_digest
        or child.scope_id != new.node_id
        or child.parent_scope_id != candidate.scope_id
    ):
        return _conflict("nested child manifest identity")
    return (
        LegacyManifestDisposition.PROVEN,
        ("nested child manifest digest and scope",),
        (),
        (),
        current.branch,
        current.worktree,
    )


def _prove_publication_and_review(
    old: ManifestNode,
    new: ManifestNode,
    current: SliceState,
    spawn_branch: str | None,
) -> tuple[LegacyManifestDisposition, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    if new.kind == "worker":
        return LegacyManifestDisposition.PROVEN, (), (), ()
    if current.status not in {
        SliceStatus.IN_REVIEW,
        SliceStatus.REPAIRING,
        SliceStatus.MERGED,
    } and not any(
        (
            current.pr_number,
            current.reviewed_head,
            current.publication,
            current.handoff,
            current.review_evidence,
        )
    ):
        return LegacyManifestDisposition.PROVEN, ("spawned state precedes publication",), (), ()
    publication, handoff, review = current.publication, current.handoff, current.review_evidence
    missing: list[str] = []
    if current.pr_number is None or current.reviewed_head is None:
        missing.append("PR and reviewed head")
    if publication is None:
        missing.append("publication binding")
    if handoff is None:
        missing.append("handoff evidence")
    if review is None:
        missing.append("review evidence")
    if missing:
        return LegacyManifestDisposition.MISSING_EVIDENCE, (), tuple(missing), ()
    assert publication is not None and handoff is not None and review is not None
    conflicts: list[str] = []
    heads = {current.reviewed_head, publication.head_sha, handoff.head_sha, review.head_sha}
    if (
        len(heads) != 1
        or publication.pr_number != current.pr_number
        or handoff.pr_number != current.pr_number
        or review.pr_number != current.pr_number
    ):
        conflicts.append("PR/head identity")
    if publication.base_branch != new.parent_integration_target:
        conflicts.append("parent integration target")
    if not spawn_branch or publication.head_branch != spawn_branch:
        conflicts.append("publication head branch")
    invocation_id = _invocation_id(current)
    if invocation_id and any(
        value != invocation_id for value in (publication.invocation_id, handoff.invocation_id)
    ):
        conflicts.append("invocation binding")
    if (
        not review.reviewer_account_authenticated
        or review.dismissed
        or review.forgejo_stale
        or review.reviewer_identity_unresolved
    ):
        conflicts.append("authenticated active review")
    if conflicts:
        return LegacyManifestDisposition.CONFLICTING_EVIDENCE, (), (), tuple(conflicts)
    return (
        LegacyManifestDisposition.PROVEN,
        ("publication, handoff, and review exact-head identity",),
        (),
        (),
    )


_DECLARATION_KEYS = (
    "context",
    "read_first",
    "steps",
    "verify",
    "done_criteria",
    "task_timeout_seconds",
)


def _declaration_conflicts(
    node: ManifestNode,
    current: SliceState,
    arguments: Mapping[str, object],
) -> tuple[str, ...]:
    """Require every meaningful plan declaration to have durable evidence."""
    conflicts: list[str] = []
    declaration = node.declaration
    for key in _DECLARATION_KEYS:
        expected = declaration.get(key)
        observed = arguments.get(key)
        if key in arguments:
            if not _same(observed, expected):
                conflicts.append(f"declaration {key}")
            continue
        if key == "done_criteria":
            observed = _review_done_criteria(getattr(current, "review_contract", None))
            if observed is not None:
                if not _same(observed, expected):
                    conflicts.append("declaration done_criteria")
            elif _meaningful(expected):
                conflicts.append("declaration done_criteria missing")
        elif key == "task_timeout_seconds":
            observed = getattr(current, key, None)
            if _meaningful(expected) and not _same(observed, expected):
                conflicts.append("declaration task_timeout_seconds")
        elif _meaningful(expected):
            conflicts.append(f"declaration {key} missing")
    return tuple(conflicts)


def _review_done_criteria(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, Mapping):
        return None
    criteria = value.get("acceptance_criteria")
    if not isinstance(criteria, (list, tuple)):
        return None
    prefix = "DONE CRITERIA: "
    return tuple(
        item.removeprefix(prefix)
        for item in criteria
        if isinstance(item, str) and item.startswith(prefix)
    )


def _meaningful(value: object) -> bool:
    return value not in (None, "", [], (), {})


def _prove_action(
    current: SliceState, entries: Sequence[Mapping[str, object]]
) -> tuple[LegacyManifestDisposition, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    action = current.action
    if action is None:
        return LegacyManifestDisposition.PROVEN, (), (), ()
    operation = {
        "dispatch": "spawn_leaf",
        "publish": "file_pr",
        "merge": "merge_pr",
        "repair": "resume_pr",
        "reviewer_spawn": "spawn_reviewer",
    }.get(action.kind.value)
    if action.kind.value == "dispatch":
        return (
            LegacyManifestDisposition.PROVEN,
            ("dispatch action is bound to spawn evidence",),
            (),
            (),
        )
    if operation is None or not action.intent_id:
        return _missing("recognized action identity")
    matches = [
        entry
        for entry in entries
        if entry.get("operation") == operation
        and entry.get("target") == current.id
        and entry.get("key") == action.intent_id
    ]
    if not matches:
        return _missing("matching action journal entry")
    if len(matches) != 1:
        return _conflict("duplicate action journal entries")
    args = _mapping(matches[0].get("arguments"))
    if (
        current.pr_number is not None
        and operation == "merge_pr"
        and args.get("pr_number") != current.pr_number
    ):
        return _conflict("merge PR identity")
    expected_head = args.get("expected_head_sha")
    if operation == "merge_pr" and expected_head != current.reviewed_head:
        return _conflict("merge exact-head identity")
    return LegacyManifestDisposition.PROVEN, (f"{operation} action journal identity",), (), ()


def _proof(
    old: ManifestNode,
    new: ManifestNode | None,
    disposition: LegacyManifestDisposition,
    *,
    evidence: tuple[str, ...] = (),
    missing: tuple[str, ...] = (),
    conflicts: tuple[str, ...] = (),
    branch: str | None = None,
    worktree: str | None = None,
) -> LegacyNodeProof:
    return LegacyNodeProof(
        old.node_id,
        new.node_id if new else None,
        old.name,
        disposition,
        evidence,
        missing,
        conflicts,
        branch,
        worktree,
    )


def _overall_disposition(proofs: Sequence[LegacyNodeProof]) -> LegacyManifestDisposition:
    dispositions = {proof.disposition for proof in proofs}
    if LegacyManifestDisposition.AMBIGUOUS in dispositions:
        return LegacyManifestDisposition.AMBIGUOUS
    if not proofs:
        return LegacyManifestDisposition.PROVEN
    if LegacyManifestDisposition.CONFLICTING_EVIDENCE in dispositions:
        return LegacyManifestDisposition.CONFLICTING_EVIDENCE
    if LegacyManifestDisposition.MISSING_EVIDENCE in dispositions:
        return LegacyManifestDisposition.MISSING_EVIDENCE
    return LegacyManifestDisposition.PROVEN


def _failure(
    proofs: tuple[LegacyNodeProof, ...],
    reason: str,
    *,
    ambiguous: bool = False,
    conflict: bool = False,
) -> LegacyManifestReconciliation:
    disposition = (
        LegacyManifestDisposition.AMBIGUOUS
        if ambiguous
        else LegacyManifestDisposition.CONFLICTING_EVIDENCE
        if conflict
        else LegacyManifestDisposition.MISSING_EVIDENCE
    )
    return LegacyManifestReconciliation(disposition, {}, proofs, reason)


def _missing(*values: str):
    return LegacyManifestDisposition.MISSING_EVIDENCE, (), values, (), None, None


def _conflict(*values: str):
    return LegacyManifestDisposition.CONFLICTING_EVIDENCE, (), (), values, None, None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _nested_text(entry: Mapping[str, object], key: str) -> str | None:
    value: object = entry
    for item in (key,):
        if isinstance(value, Mapping):
            value = value.get(item)
        else:
            value = None
    if isinstance(value, str) and value:
        return value
    nested = entry.get("result")
    if isinstance(nested, Mapping):
        value = nested.get(key)
        if isinstance(value, str) and value:
            return value
        result = nested.get("result")
        if isinstance(result, Mapping) and isinstance(result.get(key), str):
            return result[key]
    return None


def _branch_matches(observed: str, current: str | None, declared: str) -> bool:
    return (
        observed == current
        if current
        else observed == declared or observed.startswith(declared + "-")
    )


def _invocation_id(current: SliceState) -> str | None:
    if current.dispatch_invocation_id:
        return current.dispatch_invocation_id
    if current.publication is not None and current.publication.invocation_id:
        return current.publication.invocation_id
    if current.handoff is not None:
        return current.handoff.invocation_id
    return None


def _same(left: object, right: object) -> bool:
    return _plain(left) == _plain(right)


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _is_legacy_root(manifest: PlanManifest) -> bool:
    return manifest.role == "root" and manifest.owned_branch == "legacy"
