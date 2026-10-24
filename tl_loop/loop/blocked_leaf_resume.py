"""Read-only identity proof for controller-owned blocked-leaf resumes."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class BlockedLeafOwnerError(ValueError):
    """Raised when the local owner cannot be bound to a blocked plan leaf."""


@dataclass(frozen=True)
class BlockedLeafOwner:
    """Verified current owner coordinates required by the host resume tool."""

    directory: Path
    agent_name: str
    slice_id: str
    issue_id: int
    prior_invocation_id: str
    generation: int
    runtime: str
    model: str | None
    effort: str | None
    branch: str
    parent_branch: str
    worktree: Path
    worktree_recorded: str
    worktree_fingerprint: str


def resolve_dormant_owner(
    project_root: str | Path,
    *,
    slice_id: str,
    issue_id: int,
    expected_parent_branch: str,
) -> BlockedLeafOwner:
    """Find exactly one terminal no-PR owner and fingerprint its real worktree."""
    root = Path(project_root).resolve(strict=True)
    matches = _owners_for_issue(root, slice_id, issue_id)
    if len(matches) != 1:
        raise BlockedLeafOwnerError(
            f"issue #{issue_id} and slice {slice_id!r} resolve to {len(matches)} owners"
        )
    directory, identity = matches[0]
    return _owner_from_records(
        root,
        directory,
        identity,
        slice_id=slice_id,
        issue_id=issue_id,
        expected_parent_branch=expected_parent_branch,
    )


def resolve_dormant_owner_for_slice(
    project_root: str | Path,
    *,
    slice_id: str,
    expected_issue_id: int | None,
    expected_parent_branch: str,
) -> BlockedLeafOwner:
    """Resolve one dormant owner from its exact slice binding and issue record."""
    root = Path(project_root).resolve(strict=True)
    owners_root = root / ".exo" / "agents"
    try:
        directories = tuple(path for path in owners_root.iterdir() if path.is_dir())
    except OSError as error:
        raise BlockedLeafOwnerError(f"owner registry is unavailable: {error}") from error
    matches: list[tuple[Path, Mapping[str, object]]] = []
    for directory in directories:
        identity = _read_object(directory / "identity.json", "owner identity")
        if identity.get("slice_id") == slice_id and identity.get("slug") == slice_id:
            matches.append((directory, identity))
    if len(matches) != 1:
        raise BlockedLeafOwnerError(f"slice {slice_id!r} resolves to {len(matches)} logical owners")
    directory, identity = matches[0]
    try:
        issue_id = int((directory / "active_issue").read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as error:
        raise BlockedLeafOwnerError(
            f"owner {directory.name!r} has no readable issue binding"
        ) from error
    if issue_id <= 0 or (expected_issue_id is not None and issue_id != expected_issue_id):
        raise BlockedLeafOwnerError("owner issue differs from the accepted leaf declaration")
    return _owner_from_records(
        root,
        directory,
        identity,
        slice_id=slice_id,
        issue_id=issue_id,
        expected_parent_branch=expected_parent_branch,
    )


def blocked_resume_intent_id(owner: BlockedLeafOwner) -> str:
    """Match the host's idempotency key for this exact blocked-owner request."""
    payload = (
        f"blocked-resume-v1\0{owner.agent_name}\0{owner.issue_id}\0"
        f"{owner.prior_invocation_id}\0{owner.branch}\0"
        f"{owner.worktree_recorded}\0{owner.worktree_fingerprint}"
    )
    return "blocked-resume:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_owner_for_resume_retry(
    project_root: str | Path,
    *,
    slice_id: str,
    issue_id: int,
    expected_parent_branch: str,
    expected_agent_name: str,
    prior_invocation_id: str,
    prior_generation: int,
    expected_branch: str,
    expected_worktree: str,
    expected_worktree_recorded: str,
    expected_worktree_fingerprint: str,
    dispatch_intent_id: str,
) -> tuple[BlockedLeafOwner, Mapping[str, object] | None]:
    """Rebind the journaled owner before replaying an interrupted host request."""
    root = Path(project_root).resolve(strict=True)
    directory = root / ".exo" / "agents" / expected_agent_name
    identity = _read_object(directory / "identity.json", "owner identity")
    if identity.get("agent_name") != expected_agent_name:
        raise BlockedLeafOwnerError("owner registry path conflicts with the journaled identity")
    if _read_issue_id(directory) != issue_id:
        raise BlockedLeafOwnerError("journaled owner issue binding changed")
    if identity.get("slice_id") != slice_id or identity.get("slug") != slice_id:
        raise BlockedLeafOwnerError("journaled owner is bound to a different leaf scope")
    if identity.get("parent_branch") != expected_parent_branch:
        raise BlockedLeafOwnerError("journaled owner has a conflicting parent branch")
    if identity.get("birth_branch") != expected_branch:
        raise BlockedLeafOwnerError("journaled owner has a conflicting branch")
    recorded_worktree = _required_text(identity, "working_dir", "owner identity")
    worktree = _owner_worktree(root, identity)
    if recorded_worktree != expected_worktree_recorded or str(worktree) != expected_worktree:
        raise BlockedLeafOwnerError("journaled owner has a conflicting worktree")
    _validate_git_branch(worktree, expected_branch)
    invocation = _read_object(directory / "invocation.json", "owner invocation")
    current_id = _required_text(invocation, "invocation_id", "owner invocation")
    current_generation = invocation.get("generation")
    if type(current_generation) is not int:
        raise BlockedLeafOwnerError("owner invocation generation is invalid")
    if current_id == prior_invocation_id:
        if (
            current_generation != prior_generation
            or invocation.get("status") not in {"failed", "exited", "killed"}
            or invocation.get("pr_number") is not None
            or invocation.get("head_sha") is not None
            or _worktree_fingerprint(worktree) != expected_worktree_fingerprint
        ):
            raise BlockedLeafOwnerError(
                "prior owner changed after the resume request was journaled"
            )
        successor: Mapping[str, object] | None = None
    else:
        if (
            invocation.get("prior_invocation_id") != prior_invocation_id
            or current_generation != prior_generation + 1
            or invocation.get("trigger") != "resume_blocked"
        ):
            raise BlockedLeafOwnerError("current owner is not the journaled blocked-leaf successor")
        persisted_intent = _required_text(
            {"intent": _read_text(directory / "dispatch_intent", "owner dispatch intent")},
            "intent",
            "owner dispatch intent",
        )
        if persisted_intent != dispatch_intent_id:
            raise BlockedLeafOwnerError("successor belongs to a different resume request")
        if (
            invocation.get("branch") != expected_branch
            or Path(_required_text(invocation, "worktree", "owner invocation")).resolve()
            != worktree
        ):
            raise BlockedLeafOwnerError("successor changed its branch or worktree")
        successor = invocation
    return (
        BlockedLeafOwner(
            directory=directory,
            agent_name=expected_agent_name,
            slice_id=slice_id,
            issue_id=issue_id,
            prior_invocation_id=prior_invocation_id,
            generation=prior_generation,
            runtime=_required_text(identity, "agent_type", "owner identity"),
            model=_optional_text(identity.get("model")),
            effort=_optional_text(identity.get("effort")),
            branch=expected_branch,
            parent_branch=expected_parent_branch,
            worktree=worktree,
            worktree_recorded=recorded_worktree,
            worktree_fingerprint=expected_worktree_fingerprint,
        ),
        successor,
    )


def validate_resumed_owner(
    owner: BlockedLeafOwner,
    *,
    invocation_id: str,
    dispatch_intent_id: str,
    runtime: str,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """Verify that host-persisted records describe this exact successor."""
    identity = _read_object(owner.directory / "identity.json", "resumed owner identity")
    invocation = _read_object(owner.directory / "invocation.json", "resumed owner invocation")
    routing = _read_object(owner.directory / "routing.json", "resumed owner routing")
    try:
        persisted_intent = (owner.directory / "dispatch_intent").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise BlockedLeafOwnerError("resumed owner dispatch intent is unavailable") from error
    recorded_worktree = _required_text(identity, "working_dir", "resumed owner identity")
    worktree = _owner_worktree(owner.directory.parents[2], identity)
    if (
        identity.get("agent_name") != owner.agent_name
        or identity.get("slug") != owner.slice_id
        or identity.get("slice_id") != owner.slice_id
        or identity.get("topology") != "worktree_per_agent"
        or identity.get("birth_branch") != owner.branch
        or identity.get("parent_branch") != owner.parent_branch
        or worktree != owner.worktree
        or recorded_worktree != owner.worktree_recorded
    ):
        raise BlockedLeafOwnerError("resumed owner changed its accepted scope coordinates")
    generation = invocation.get("generation")
    model = _optional_text(identity.get("model"))
    effort = _optional_text(identity.get("effort"))
    if (
        invocation.get("invocation_id") != invocation_id
        or invocation.get("prior_invocation_id") != owner.prior_invocation_id
        or generation != owner.generation + 1
        or invocation.get("trigger") != "resume_blocked"
        or invocation.get("branch") != owner.branch
        or Path(_required_text(invocation, "worktree", "resumed invocation")).resolve()
        != owner.worktree
        or invocation.get("runtime") != runtime
        or _optional_text(invocation.get("model")) != model
        or _optional_text(invocation.get("effort")) != effort
        or identity.get("agent_type") != runtime
        or _optional_text(identity.get("model")) != model
        or _optional_text(identity.get("effort")) != effort
        or persisted_intent != dispatch_intent_id
    ):
        raise BlockedLeafOwnerError(
            "resumed invocation lineage, route, or dispatch intent conflicts"
        )
    if (
        routing.get("owner_agent_id") != owner.agent_name
        or routing.get("owner_invocation_id") != invocation_id
        or routing.get("owner_generation") != generation
    ):
        raise BlockedLeafOwnerError(
            "resumed invocation routing ownership is incomplete or conflicting"
        )
    issue_id = _read_issue_id(owner.directory)
    if issue_id != owner.issue_id:
        raise BlockedLeafOwnerError("resumed owner issue binding changed")
    _validate_git_branch(owner.worktree, owner.branch)
    return identity, invocation


def _owners_for_issue(
    project_root: Path, slice_id: str, issue_id: int
) -> list[tuple[Path, Mapping[str, object]]]:
    owners_root = project_root / ".exo" / "agents"
    try:
        directories = tuple(path for path in owners_root.iterdir() if path.is_dir())
    except OSError as error:
        raise BlockedLeafOwnerError(f"owner registry is unavailable: {error}") from error
    matches: list[tuple[Path, Mapping[str, object]]] = []
    for directory in directories:
        identity = _read_object(directory / "identity.json", "owner identity")
        if identity.get("slice_id") != slice_id or identity.get("slug") != slice_id:
            continue
        try:
            active_issue = int((directory / "active_issue").read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as error:
            raise BlockedLeafOwnerError(
                f"owner {directory.name!r} has no readable active issue binding"
            ) from error
        if active_issue == issue_id:
            matches.append((directory, identity))
    return matches


def _read_issue_id(directory: Path) -> int:
    try:
        issue_id = int((directory / "active_issue").read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as error:
        raise BlockedLeafOwnerError("owner has no readable active issue binding") from error
    if issue_id <= 0:
        raise BlockedLeafOwnerError("owner issue binding is invalid")
    return issue_id


def _owner_from_records(
    project_root: Path,
    directory: Path,
    identity: Mapping[str, object],
    *,
    slice_id: str,
    issue_id: int,
    expected_parent_branch: str,
) -> BlockedLeafOwner:
    agent_name = _required_text(identity, "agent_name", "owner identity")
    if directory.name != agent_name:
        raise BlockedLeafOwnerError("owner registry path does not match its agent identity")
    if identity.get("topology") != "worktree_per_agent":
        raise BlockedLeafOwnerError("blocked leaf owner is not isolated in its own worktree")
    branch = _required_text(identity, "birth_branch", "owner identity")
    parent_branch = _required_text(identity, "parent_branch", "owner identity")
    if parent_branch != expected_parent_branch:
        raise BlockedLeafOwnerError("owner parent branch differs from the accepted child scope")
    if branch != f"{parent_branch}.{agent_name}":
        raise BlockedLeafOwnerError(
            "owner branch does not match its registered parent and agent identity"
        )
    worktree = _owner_worktree(project_root, identity)
    invocation = _read_object(directory / "invocation.json", "owner invocation")
    invocation_id = _required_text(invocation, "invocation_id", "owner invocation")
    generation = invocation.get("generation")
    if type(generation) is not int or generation < 1:
        raise BlockedLeafOwnerError("owner invocation generation is invalid")
    if invocation.get("status") not in {"failed", "exited", "killed"}:
        raise BlockedLeafOwnerError("owner invocation is active or has an unresolved outcome")
    if invocation.get("pr_number") is not None or invocation.get("head_sha") is not None:
        raise BlockedLeafOwnerError("owner has publication evidence and must use PR continuation")
    if invocation.get("slice_id") not in {None, slice_id}:
        raise BlockedLeafOwnerError("owner invocation is bound to a different slice")
    if invocation.get("branch") != branch or _invocation_worktree(invocation) != str(worktree):
        raise BlockedLeafOwnerError(
            "owner invocation branch or worktree conflicts with its identity"
        )
    _validate_git_branch(worktree, branch)
    fingerprint = _worktree_fingerprint(worktree)
    return BlockedLeafOwner(
        directory=directory,
        agent_name=agent_name,
        slice_id=slice_id,
        issue_id=issue_id,
        prior_invocation_id=invocation_id,
        generation=generation,
        runtime=_required_text(invocation, "runtime", "owner invocation"),
        model=_optional_text(invocation.get("model")),
        effort=_optional_text(invocation.get("effort")),
        branch=branch,
        parent_branch=parent_branch,
        worktree=worktree,
        worktree_recorded=_required_text(identity, "working_dir", "owner identity"),
        worktree_fingerprint=fingerprint,
    )


def _owner_worktree(project_root: Path, identity: Mapping[str, object]) -> Path:
    raw = _required_text(identity, "working_dir", "owner identity")
    candidate = Path(raw)
    candidate = (
        (project_root / candidate).resolve(strict=True)
        if not candidate.is_absolute()
        else candidate.resolve(strict=True)
    )
    allowed_root = (project_root / ".exo" / "worktrees").resolve(strict=True)
    if candidate != allowed_root and allowed_root not in candidate.parents:
        raise BlockedLeafOwnerError("owner worktree is outside the managed worktree root")
    if not candidate.is_dir():
        raise BlockedLeafOwnerError("owner worktree is unavailable")
    return candidate


def _validate_git_branch(worktree: Path, expected: str) -> None:
    try:
        observed = subprocess.run(
            ["git", "-C", str(worktree), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise BlockedLeafOwnerError(f"owner branch cannot be verified: {error}") from error
    if observed != expected:
        raise BlockedLeafOwnerError(
            f"owner worktree branch changed: expected {expected!r}, found {observed!r}"
        )


def _worktree_fingerprint(worktree: Path) -> str:
    try:
        status = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise BlockedLeafOwnerError(f"owner worktree status is unavailable: {error}") from error
    return "sha256:" + hashlib.sha256(status).hexdigest()


def _read_object(path: Path, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BlockedLeafOwnerError(f"{label} is unavailable or malformed") from error
    if not isinstance(value, dict):
        raise BlockedLeafOwnerError(f"{label} is not an object")
    return value


def _read_text(path: Path, label: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise BlockedLeafOwnerError(f"{label} is unavailable") from error
    if not value:
        raise BlockedLeafOwnerError(f"{label} is empty")
    return value


def _required_text(value: Mapping[str, object], key: str, label: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise BlockedLeafOwnerError(f"{label} is missing {key}")
    return result.strip()


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _invocation_worktree(invocation: Mapping[str, object]) -> str:
    value = invocation.get("worktree")
    return value if isinstance(value, str) else ""
