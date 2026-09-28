"""Durable evidence readers for the #1111 recreated-leaf acceptance.

Every function here reads a durable artifact and returns a plain record. None
of them waits, polls, or decides: the driver owns the bounded waits, and the
assertions own the verdicts. Keeping the readers free of control flow is what
lets ``test_contract.py`` exercise them against recorded artifacts instead of
against a live server.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

#: The ledger segment directory the server writes under a project.
LEDGER_SEGMENTS = Path(".exo") / "ledger" / "segments"

#: Durable per-agent identity written by the shipped spawn path.
IDENTITY = "identity.json"

#: The invocation record the server writes beside a spawned agent.
INVOCATION = "invocation.json"

#: The publication registry whose entries carry the ledger-owned proof that a
#: branch really was published by the agent that owns it.
PUBLISHED_HEADS = "published-heads.json"

#: The attach-versus-create action names the server records.
ATTACH = "attach"
CREATE_FROM_REVISION = "create_from_revision"
CREATE_FROM_BASE = "create_from_base"


class EvidenceError(RuntimeError):
    """Raised when a durable artifact cannot be read at all."""


@dataclass(frozen=True)
class Refusal:
    """One ``agent.spawn_failed`` record, reduced to its machine code.

    The server writes the code in ``code`` and the operator prose inside
    ``error``, formatted as ``"[{code}] {message}"``. Both are kept, and the
    prose is the error with the code prefix removed, so a caller can assert on
    either without re-deriving the format the server uses.
    """

    intent_id: str | None
    code: str | None
    error: str
    message: str
    child_agent: str | None


def read_json(path: Path) -> Any:
    """Read one JSON document, reporting the path when it cannot be parsed."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceError(f"could not read {path}: {error}") from error


def read_json_if_present(path: Path) -> Any | None:
    """Read one JSON document, or report its absence as ``None``."""
    if not path.is_file():
        return None
    return read_json(path)


def ledger_events(project: Path) -> list[dict[str, Any]]:
    """Return every committed ledger record for a project, in segment order."""
    segments = project / LEDGER_SEGMENTS
    if not segments.is_dir():
        raise EvidenceError(f"project has no ledger segments: {segments}")
    events: list[dict[str, Any]] = []
    for path in sorted(segments.glob("*")):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
    return events


def typed(
    events: Iterable[Mapping[str, Any]], event_type: str
) -> list[Mapping[str, Any]]:
    """Return the records of one ledger type that carry an object payload."""
    return [
        event
        for event in events
        if event.get("type") == event_type and isinstance(event.get("data"), dict)
    ]


def _payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    data = event.get("data")
    return data if isinstance(data, Mapping) else {}


def authoritative_spawns(
    events: Iterable[Mapping[str, Any]],
    *,
    branch: str | None = None,
    intent_id: str | None = None,
) -> list[Mapping[str, Any]]:
    """Return the authoritative ``agent.spawned`` records.

    The server also writes a second, non-authoritative ``agent.spawned`` from
    the WASM log path, which carries neither ``spawn_type`` nor ``branch``.
    Counting those would make a duplicate invisible, so the authoritative
    record is the one that names both.
    """
    found = [
        event
        for event in typed(events, "agent.spawned")
        if event["data"].get("spawn_type") == "leaf_subtree"
        and event["data"].get("branch")
    ]
    if branch is not None:
        found = [event for event in found if event["data"].get("branch") == branch]
    if intent_id is not None:
        found = [
            event for event in found if event["data"].get("intent_id") == intent_id
        ]
    return found


def refusals(
    events: Iterable[Mapping[str, Any]], *, intent_id: str | None = None
) -> list[Refusal]:
    """Return every recorded spawn refusal, reduced to its machine code."""
    found: list[Refusal] = []
    for event in typed(events, "agent.spawn_failed"):
        data = _payload(event)
        if intent_id is not None and data.get("intent_id") != intent_id:
            continue
        error = str(data.get("error", ""))
        found.append(
            Refusal(
                intent_id=_optional_str(data.get("intent_id")),
                code=_optional_str(data.get("code")),
                error=error,
                message=_prose(error, _optional_str(data.get("code"))),
                child_agent=_optional_str(data.get("child_agent")),
            )
        )
    return found


def _prose(error: str, code: str | None) -> str:
    """Return the operator prose with the server's ``[code]`` prefix removed."""
    prefix = f"[{code}] " if code else ""
    return error[len(prefix) :] if prefix and error.startswith(prefix) else error


def refusal_codes(
    events: Iterable[Mapping[str, Any]], *, intent_id: str | None = None
) -> list[str | None]:
    """Return just the machine codes, in ledger order."""
    return [refusal.code for refusal in refusals(events, intent_id=intent_id)]


def attach_decisions(
    events: Iterable[Mapping[str, Any]],
    *,
    branch: str | None = None,
    action: str | None = None,
) -> list[Mapping[str, Any]]:
    """Return the durable attach-versus-create decisions."""
    found = typed(events, "agent.attach_decided")
    if branch is not None:
        found = [event for event in found if _payload(event).get("branch") == branch]
    if action is not None:
        found = [event for event in found if _payload(event).get("action") == action]
    return found


def attach_completions(
    events: Iterable[Mapping[str, Any]], *, branch: str | None = None
) -> list[Mapping[str, Any]]:
    """Return the durable record of whether provisioning created or reused."""
    found = typed(events, "agent.attach_completed")
    if branch is not None:
        found = [event for event in found if _payload(event).get("branch") == branch]
    return found


def ownership_conflicts(
    events: Iterable[Mapping[str, Any]], *, branch: str | None = None
) -> list[Mapping[str, Any]]:
    """Return the durable branch-ownership conflicts, which are always terminal."""
    found = typed(events, "agent.branch_ownership_conflict")
    if branch is not None:
        found = [event for event in found if _payload(event).get("branch") == branch]
    return found


def publications(
    events: Iterable[Mapping[str, Any]], *, branch: str | None = None
) -> list[Mapping[str, Any]]:
    """Return the durable publication records."""
    found = typed(events, "pr.filed") + typed(events, "pr.published")
    if branch is None:
        return found
    return [
        event
        for event in found
        if _payload(event).get("branch") == branch
        or _payload(event).get("head_branch") == branch
    ]


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def git(project: Path, *arguments: str) -> str:
    """Run one git command in a project and return its stdout."""
    result = subprocess.run(
        ["git", "-C", str(project), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise EvidenceError(
            f"git {' '.join(arguments)} failed ({result.returncode}) in {project}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def worktrees_for_branch(project: Path, branch: str) -> list[Path]:
    """Return every registered worktree that has ``branch`` checked out.

    ``git worktree list --porcelain`` is the only record of what git itself
    considers registered, so it is what proves a cwd is a real worktree rather
    than a directory that merely looks like one.
    """
    found: list[Path] = []
    current: str | None = None
    for line in git(project, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree ") :]
        elif line.strip() == f"branch refs/heads/{branch}" and current is not None:
            found.append(Path(current))
    return found


def registered_branches(project: Path) -> dict[Path, str]:
    """Map every registered worktree path to the branch it has checked out."""
    mapping: dict[Path, str] = {}
    current: str | None = None
    branch: str | None = None
    for line in git(project, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            if current is not None and branch is not None:
                mapping[Path(current)] = branch
            current = line[len("worktree ") :]
            branch = None
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/") :]
    if current is not None and branch is not None:
        mapping[Path(current)] = branch
    return mapping


def head_of(project: Path, revision: str = "HEAD") -> str:
    """Return the commit a project or revision points at."""
    return git(project, "rev-parse", revision)


def branch_exists(project: Path, branch: str) -> bool:
    """Report whether a local branch exists."""
    return git(project, "branch", "--list", branch).strip() != ""


def agent_identity(project: Path, name: str) -> dict[str, Any] | None:
    """Return the durable identity of one agent, or report its absence."""
    value = read_json_if_present(project / ".exo" / "agents" / name / IDENTITY)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise EvidenceError(
            f"identity for {name!r} is not an object: {value!r}"
        )
    return value


def agent_identities(project: Path, name: str) -> list[dict[str, Any]]:
    """Return every durable identity recorded for one agent name.

    An agent owns exactly one identity, so more than one is itself the defect
    this reader exists to expose.
    """
    agents = project / ".exo" / "agents"
    if not agents.is_dir():
        raise EvidenceError(f"project has no agent directory: {agents}")
    found: list[dict[str, Any]] = []
    for identity_path in sorted(agents.glob(f"*/{IDENTITY}")):
        if identity_path.parent.name != name:
            continue
        value = read_json(identity_path)
        if not isinstance(value, dict):
            raise EvidenceError(f"identity for {name!r} is not an object: {value!r}")
        found.append(value)
    return found


def agent_invocation(project: Path, name: str) -> dict[str, Any] | None:
    """Return one agent's invocation record, or report its absence."""
    value = read_json_if_present(project / ".exo" / "agents" / name / INVOCATION)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise EvidenceError(f"invocation for {name!r} is not an object: {value!r}")
    return value


def published_heads(project: Path, name: str) -> list[dict[str, Any]]:
    """Return the ledger-owned publication records for one agent."""
    value = read_json_if_present(
        project / ".exo" / "agents" / name / PUBLISHED_HEADS
    )
    if value is None:
        return []
    if isinstance(value, dict):
        entries = value.get("publications", value.get("heads"))
        value = entries if isinstance(entries, list) else []
    if not isinstance(value, list):
        raise EvidenceError(
            f"published heads for {name!r} are not a list: {value!r}"
        )
    return [entry for entry in value if isinstance(entry, dict)]


def planned_worktree_exists(project: Path, path: Path) -> bool:
    """Report whether a planned worktree path exists on disk at all."""
    return path.exists()


def find_pull_number(payload: Any) -> int | None:
    """Find the pull-request number the tool surface reported.

    The spawn and publication tools answer through the WASM content envelope,
    so the number can sit at the top level or inside a nested content object.
    """
    for candidate in _objects(payload):
        number = candidate.get("pr_number")
        if type(number) is int and number > 0:
            return number
    return None


def find_field(payload: Any, key: str) -> Any:
    """Return the first value recorded for ``key`` anywhere in a tool response."""
    for candidate in _objects(payload):
        if key in candidate:
            return candidate[key]
    return None


def _objects(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _objects(child)
    elif isinstance(value, str) and value.lstrip()[:1] in "{[":
        try:
            yield from _objects(json.loads(value))
        except json.JSONDecodeError:
            return


def is_success(payload: Any) -> bool:
    """Report whether a tool response carries an explicit success."""
    for candidate in _objects(payload):
        if candidate.get("success") is True:
            return True
    return False
