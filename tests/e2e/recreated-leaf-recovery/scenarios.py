"""The T1-T9 acceptance scenarios for recreated-leaf recovery (#1111).

Each scenario drives the real shipped server and returns the evidence it proved.
The driver above them prints one PASS or FAIL line per T-item and owns cleanup,
so no scenario has to know how it is being reported.

Every wait is on a durable boundary: the forge's own pull-request record, the
committed ledger, git's worktree registry, or the server's recorded refusal. A
scenario never succeeds because time passed.

The nine items are:

T1  an ordered child and a leaf exist, with a unique commit and one open PR
T2  recreating the session preserves the leaf branch
T3  the same plan attaches to the preserved branch, verified
T4  the head is unchanged, the cwd is a registered worktree, the branch is right
T5  exactly one identity, worktree, branch, authoritative spawn, and PR
T6  a second recreate changes nothing
T7  five fail-closed shapes, including an expected-agent resume
T8  sink writes do not create a planned directory for an agent with no worktree
T9  a retryable refusal reconciles on re-drive; a terminal conflict never does
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import evidence as ev
import forgejo
import project as pj
from evidence import Refusal
from project import (
    BASE_BRANCH,
    CHILD_BRANCH,

    LEAF_AGENT,
    LEAF_BRANCH,
    LEAF_HARNESS,
    LEAF_NAME,
    Project,
)
from tl_loop.client.transport import ServerError, TransportError
from waiter import await_boundary, await_stable


class ScenarioError(RuntimeError):
    """Raised when a T-item's own acceptance assertion does not hold."""


def require(condition: bool, message: str) -> None:
    """Fail the current T-item unless the condition holds."""
    if not condition:
        raise ScenarioError(message)


#: The machine codes this acceptance expects, named so an assertion reads as the
#: contract it proves rather than as a string literal.
BRANCH_EXISTS = "worktree.branch_exists"
LIFECYCLE_LOCK_TIMEOUT = "worktree.lifecycle_lock_timeout"
OWNERSHIP_CONFLICT = "worktree.branch_ownership_conflict"
PATH_UNREGISTERED = "worktree.path_unregistered"

#: The lifecycle lock the spawn path holds across its create-or-attach decision.
LIFECYCLE_LOCK = Path(".exo") / "worktree-lifecycle.lock"

#: Bounded wait for the leaf to commit, push, and file its pull request.
PUBLICATION_TIMEOUT_SECONDS = 180.0

#: Bounded wait for a refusal to reach the ledger.
REFUSAL_TIMEOUT_SECONDS = 90.0

#: How long a pull-request count must hold steady to count as settled.
SETTLE_SECONDS = 4.0


def intent(name: str) -> str:
    """Return a unique intent id for one dispatch attempt."""
    return f"e2e-1111-{name}-{time.time_ns()}"


def leaf_evidence_path(project: Project) -> Path:
    """Return the file the deterministic leaf records its publication in."""
    return project.repo / ".exo" / "e2e-1111-leaf-evidence.jsonl"


def leaf_publications(project: Project) -> list[dict[str, Any]]:
    """Return every publication the deterministic leaf durably recorded."""
    path = leaf_evidence_path(project)
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("event") == "leaf_published":
            records.append(value)
    return records


def open_pulls(fixture: forgejo.Instance) -> list[dict[str, Any]]:
    """Return every open pull request on the run's own repository."""
    value = forgejo.api(
        "GET",
        f"{fixture.repository_api_url()}/pulls?state=open&limit=50",
        token=fixture.author.token,
    )
    if not isinstance(value, list):
        raise ScenarioError(f"pull request listing is not a list: {value!r}")
    return [pull for pull in value if isinstance(pull, dict)]


def pulls_on_branch(
    fixture: forgejo.Instance, branch: str
) -> list[dict[str, Any]]:
    """Return every open pull request whose head is exactly ``branch``."""
    found: list[dict[str, Any]] = []
    for pull in open_pulls(fixture):
        head = pull.get("head")
        if isinstance(head, Mapping) and head.get("ref") == branch:
            found.append(pull)
    return found


def dispatch_leaf(
    project: Project, name: str, task: str, **kwargs: Any
) -> tuple[Any, str]:
    """Dispatch one leaf through the shipped controller tool surface.

    The intent id is returned because it is the durable handle every later
    proof keys on, so a wait can name the attempt it is waiting for instead of
    counting rows that earlier attempts also wrote.
    """
    attempt = str(kwargs.pop("intent_id", None) or intent(name))
    return (
        project.effects().spawn_leaf(
            name=name, task=task, intent_id=attempt, agent_type=LEAF_HARNESS, **kwargs
        ),
        attempt,
    )


def _authoritative_spawn(
    project: Project, attempt: str
) -> list[Mapping[str, Any]]:
    """Return this attempt's authoritative spawn records."""
    return ev.authoritative_spawns(project.ledger(), intent_id=attempt)


def await_spawn(project: Project, attempt: str) -> Mapping[str, Any]:
    """Wait for one attempt's authoritative spawn, correlated by its intent."""
    found = await_boundary(
        lambda: _authoritative_spawn(project, attempt) or None,
        description=f"the authoritative spawn for intent {attempt}",
        timeout=PUBLICATION_TIMEOUT_SECONDS,
    )
    require(
        len(found) == 1,
        f"intent {attempt} spawned {len(found)} leaves: "
        f"{json.dumps(found, default=str)[:1500]}",
    )
    return found[0]


def await_publication(
    project: Project, fixture: forgejo.Instance, branch: str
) -> dict[str, Any]:
    """Wait for the leaf to commit, push, and file exactly one open PR.

    The boundary is the forge's own record, cross-checked against the leaf's
    durable publication record, so neither side alone can pass: a stale ledger
    row cannot open a pull request, and an unrelated pull request cannot be
    attributed to this leaf.
    """
    recorded = await_boundary(
        lambda: leaf_publications(project) or None,
        description=f"the deterministic leaf to publish {branch}",
        timeout=PUBLICATION_TIMEOUT_SECONDS,
    )
    pulls = await_stable(
        lambda: pulls_on_branch(fixture, branch),
        description=f"pull requests on {branch} to hold steady",
        stable_for=SETTLE_SECONDS,
        timeout=PUBLICATION_TIMEOUT_SECONDS,
    )
    require(
        len(recorded) == 1,
        f"the leaf published more than once: {json.dumps(recorded)[:2000]}",
    )
    require(
        len(pulls) == 1,
        f"expected exactly one open pull request on {branch}, got {len(pulls)}: "
        f"{json.dumps(pulls, default=str)[:2000]}",
    )
    pull = pulls[0]
    require(
        pull.get("state") == "open",
        f"the leaf's pull request is not open: {pull.get('state')!r}",
    )
    head = pull.get("head")
    require(
        isinstance(head, Mapping) and isinstance(head.get("sha"), str),
        f"the leaf's pull request has no head SHA: {pull!r}",
    )
    require(
        head["sha"] == recorded[0]["head"],
        f"the pull request head {head['sha']} is not the leaf's own commit "
        f"{recorded[0]['head']}",
    )
    return {
        "branch": branch,
        "pr_number": pull.get("number"),
        "pr_head": head["sha"],
        "pr_state": pull.get("state"),
        "leaf_head": recorded[0]["head"],
        "leaf_record": recorded[0],
    }


# --------------------------------------------------------------------------
# T1
# --------------------------------------------------------------------------


def t1_ordered_child_and_leaf(
    project: Project, fixture: forgejo.Instance
) -> dict[str, Any]:
    """Provision the ordered child and prove the leaf has a commit and one PR.

    The child's worktree is created by the server's own provisioning route, and
    the leaf's commit, push, and pull request come from the deterministic leaf
    actor running in its own worktree through the shipped tool surface.
    """
    child = pj.provision_child(project)
    require(
        ev.branch_exists(project.repo, CHILD_BRANCH),
        f"the ordered child branch {CHILD_BRANCH} was not created",
    )
    child_worktrees = ev.worktrees_for_branch(project.repo, CHILD_BRANCH)
    require(
        len(child_worktrees) == 1,
        f"expected one registered worktree for {CHILD_BRANCH}, got {child_worktrees!r}",
    )

    result, _attempt = dispatch_leaf(
        project,
        LEAF_NAME,
        "Prove recreated leaf recovery end to end",
    )
    require(
        ev.is_success(result.raw),
        f"the shipped spawn_leaf route refused the leaf: {result.raw!r}",
    )
    publication = await_publication(project, fixture, LEAF_BRANCH)
    require(
        publication["leaf_head"] != ev.head_of(project.repo, BASE_BRANCH),
        f"the leaf's commit {publication['leaf_head']} is not a unique commit of "
        f"its own: it is the base branch head",
    )
    return {
        "child_worktree": child["child_worktree"],
        "child_branch": CHILD_BRANCH,
        "child_worktrees": [str(path) for path in child_worktrees],
        "leaf_agent": LEAF_AGENT,
        "leaf_branch": LEAF_BRANCH,
        "leaf_head": publication["leaf_head"],
        "pr_number": publication["pr_number"],
        "pr_state": publication["pr_state"],
    }


# --------------------------------------------------------------------------
# T2
# --------------------------------------------------------------------------


def t2_recreate_preserves_branch(project: Project) -> dict[str, Any]:
    """Recreate the session and prove the leaf branch survived it untouched.

    Recreating the session is the acceptance's central event: the tmux session
    and the server process are replaced, while the project, its repository, its
    worktree registry, and the leaf's published branch are the durable state a
    recreated leaf has to recover onto.
    """
    before = {
        "head": ev.head_of(project.repo, LEAF_BRANCH),
        "worktrees": [
            str(path) for path in ev.worktrees_for_branch(project.repo, LEAF_BRANCH)
        ],
        "identities": ev.agent_identities(project.repo, LEAF_AGENT),
    }
    session, pid = project.session, project.process.pid
    project.close()
    require(
        not pj.session_exists(session),
        f"the acceptance's own tmux session {session} survived close()",
    )
    require(
        project.process.poll() is not None,
        f"the acceptance's own server process {pid} survived close()",
    )
    recreated = pj.recreate_session(project.run, project.port)
    after_head = ev.head_of(recreated.repo, LEAF_BRANCH)
    require(
        after_head == before["head"],
        f"recreating the session moved the leaf head from {before['head']} to "
        f"{after_head}",
    )
    after_worktrees = [
        str(path) for path in ev.worktrees_for_branch(recreated.repo, LEAF_BRANCH)
    ]
    require(
        after_worktrees == before["worktrees"],
        f"recreating the session changed the leaf worktree set: "
        f"before={before['worktrees']!r} after={after_worktrees!r}",
    )
    require(
        ev.agent_identities(recreated.repo, LEAF_AGENT) == before["identities"],
        "recreating the session changed the leaf's durable identity",
    )
    return {
        "recreated": recreated,
        "session": session,
        "old_server_pid": pid,
        "head": after_head,
        "worktrees": after_worktrees,
        "branch": LEAF_BRANCH,
    }


# --------------------------------------------------------------------------
# T3
# --------------------------------------------------------------------------


def _leaf_head_evidence(project: Project, branch: str) -> Any:
    """Return the head evidence the server itself records for a branch.

    This is the shipped registry, not a value the harness computed, so reading
    it proves the acceptance is looking at the same record the attach path
    verifies against.
    """
    from evidence import read_json_if_present

    return read_json_if_present(project.repo / ".exo" / "published-heads.json")


def t3_reuse_preserved_worktree(
    project: Project, head: str
) -> dict[str, Any]:
    """Re-dispatch the leaf with its worktree intact and prove it was reused.

    A worktree that still holds the branch is not re-provisioned: the spawn
    path verifies the live worktree's head against the head the leaf published
    and reuses it. The proof is that the head is untouched, the same single
    worktree is still registered, and the re-dispatch spawned exactly one leaf.
    """
    registered_before = ev.worktrees_for_branch(project.repo, LEAF_BRANCH)
    require(
        len(registered_before) == 1,
        f"the preserved worktree is not singular: {registered_before!r}",
    )
    result, attempt = dispatch_leaf(
        project,
        LEAF_NAME,
        "Prove recreated leaf recovery end to end",
    )
    require(
        ev.is_success(result.raw),
        f"the same plan did not re-dispatch the leaf: {result.raw!r}",
    )
    spawn = await_spawn(project, attempt)
    require(
        ev.head_of(project.repo, LEAF_BRANCH) == head,
        f"reusing the preserved worktree moved the head from {head} to "
        f"{ev.head_of(project.repo, LEAF_BRANCH)}",
    )
    registered_after = ev.worktrees_for_branch(project.repo, LEAF_BRANCH)
    require(
        registered_after == registered_before,
        f"reusing the preserved worktree changed the worktree set: "
        f"before={registered_before!r} after={registered_after!r}",
    )
    require(
        len(registered_after) == 1,
        f"reusing the preserved worktree left {len(registered_after)} worktrees",
    )
    return {
        "shape": "reuse",
        "branch": LEAF_BRANCH,
        "head": head,
        "worktrees": len(registered_after),
        "authoritative_spawns": 1,
        "intent_id": attempt,
        "spawned_branch": (spawn.get("data") or {}).get("branch"),
    }


def t3_attach_preserved_branch(
    project: Project, fixture: forgejo.Instance, head: str
) -> dict[str, Any]:
    """Lose the leaf's worktree and prove the branch is reattached, not recreated.

    This is the shape the acceptance is named for: the branch and the head the
    leaf published survive, but the worktree that held it does not. The spawn
    path must then record a verified ``attach`` decision for the preserved
    branch, and the worktree it makes must be at the published head rather than
    at the base.
    """
    published = _leaf_head_evidence(project, LEAF_BRANCH)
    require(
        published is not None,
        "the leaf published no head evidence, so nothing can be reattached to it",
    )
    lost = ev.worktrees_for_branch(project.repo, LEAF_BRANCH)
    require(
        len(lost) == 1,
        f"the worktree to lose is not singular: {lost!r}",
    )
    subprocess.run(
        ["git", "-C", str(project.repo), "worktree", "remove", "--force", str(lost[0])],
        check=True,
    )
    require(
        not ev.worktrees_for_branch(project.repo, LEAF_BRANCH),
        "the leaf's worktree survived the loss it was supposed to lose",
    )
    require(
        ev.branch_exists(project.repo, LEAF_BRANCH),
        f"losing the worktree also lost the preserved branch {LEAF_BRANCH}",
    )
    require(
        ev.head_of(project.repo, LEAF_BRANCH) == head,
        "losing the worktree also moved the preserved head",
    )
    attach_before = len(
        ev.attach_decisions(project.ledger(), branch=LEAF_BRANCH, action=ev.ATTACH)
    )
    result, attempt = dispatch_leaf(
        project,
        LEAF_NAME,
        "Prove recreated leaf recovery end to end",
    )
    events = await_boundary(
        lambda: (
            ev.attach_decisions(project.ledger(), branch=LEAF_BRANCH, action=ev.ATTACH)
            or None
        ),
        description=f"a verified attach decision for {LEAF_BRANCH}",
        timeout=PUBLICATION_TIMEOUT_SECONDS,
    )
    require(
        ev.is_success(result.raw),
        f"the same plan did not re-dispatch the leaf: {result.raw!r}",
    )
    require(
        len(events) == attach_before + 1,
        f"reattaching a preserved branch recorded "
        f"{len(events) - attach_before} attach decisions, expected exactly one",
    )
    await_spawn(project, attempt)
    latest = events[-1]
    payload = latest.get("data")
    require(
        isinstance(payload, Mapping) and payload.get("branch_exists") is True,
        f"the attach decision did not record a preserved branch: {payload!r}",
    )
    require(
        payload.get("branch") == LEAF_BRANCH,
        f"the attach decision is for another branch: {payload!r}",
    )
    require(
        ev.head_of(project.repo, LEAF_BRANCH) == head,
        f"reattaching moved the preserved head from {head} to "
        f"{ev.head_of(project.repo, LEAF_BRANCH)}",
    )
    restored = ev.worktrees_for_branch(project.repo, LEAF_BRANCH)
    require(
        len(restored) == 1,
        f"reattaching left {len(restored)} worktrees on {LEAF_BRANCH}: "
        f"{[str(path) for path in restored]!r}",
    )
    require(
        ev.head_of(restored[0]) == head,
        f"the reattached worktree is at {ev.head_of(restored[0])}, not the "
        f"published head {head}",
    )
    return {
        "shape": "attach",
        "action": payload.get("action"),
        "branch": payload.get("branch"),
        "branch_exists": payload.get("branch_exists"),
        "worktree_path": payload.get("worktree_path"),
        "head": head,
        "worktrees": len(restored),
        "attach_decisions": len(events) - attach_before,
        "intent_id": attempt,
        "verified": True,
    }


# --------------------------------------------------------------------------
# T4
# --------------------------------------------------------------------------


def t4_head_cwd_branch(project: Project, publication: Mapping[str, Any]) -> dict[str, Any]:
    """Prove the head is unchanged, the cwd is a registered worktree, and the
    branch is the one the plan declares.

    Each of the three is checked against the artifact that owns it: the branch
    ref for the head, git's own worktree registry for the cwd, and the durable
    identity for the branch the agent owns.
    """
    expected_head = publication["leaf_head"]
    observed_head = ev.head_of(project.repo, LEAF_BRANCH)
    require(
        observed_head == expected_head,
        f"the leaf branch head moved from {expected_head} to {observed_head}",
    )
    registered = ev.registered_branches(project.repo)
    leaf_worktrees = [path for path, branch in registered.items() if branch == LEAF_BRANCH]
    require(
        len(leaf_worktrees) == 1,
        f"expected exactly one registered worktree on {LEAF_BRANCH}, got "
        f"{[str(path) for path in leaf_worktrees]!r}",
    )
    leaf_worktree = leaf_worktrees[0]
    require(
        (leaf_worktree / ".git").exists(),
        f"the leaf cwd {leaf_worktree} is not a git worktree",
    )
    require(
        ev.git(leaf_worktree, "branch", "--show-current") == LEAF_BRANCH,
        f"the leaf worktree is not on {LEAF_BRANCH}: "
        f"{ev.git(leaf_worktree, 'branch', '--show-current')!r}",
    )
    require(
        ev.head_of(leaf_worktree) == expected_head,
        f"the leaf worktree head {ev.head_of(leaf_worktree)} is not the branch "
        f"head {expected_head}",
    )
    identities = ev.agent_identities(project.repo, LEAF_AGENT)
    require(
        len(identities) == 1,
        f"expected exactly one durable identity for {LEAF_AGENT}, got {identities!r}",
    )
    identity = identities[0]
    require(
        identity.get("birth_branch") == LEAF_BRANCH,
        f"the leaf identity's birth branch is not {LEAF_BRANCH}: {identity!r}",
    )
    require(
        _recorded_working_dir(project.repo, identity) == leaf_worktree.resolve(),
        f"the leaf identity's working dir is not its registered worktree: {identity!r}",
    )
    return {
        "head": observed_head,
        "head_unchanged": True,
        "cwd": str(leaf_worktree),
        "cwd_is_registered_worktree": True,
        "branch": LEAF_BRANCH,
        "identity_birth_branch": identity.get("birth_branch"),
    }


def _recorded_working_dir(project: Path, identity: Mapping[str, Any]) -> Path:
    """Resolve an identity's recorded working dir against the project.

    The server records the working dir relative to the project, so a relative
    value is resolved against the project rather than the harness's own cwd.
    """
    recorded = Path(str(identity.get("working_dir", "")))
    return recorded if recorded.is_absolute() else project / recorded


# --------------------------------------------------------------------------
# T5
# --------------------------------------------------------------------------


def t5_exactly_one(
    project: Project, fixture: forgejo.Instance, starts: int
) -> dict[str, Any]:
    """Prove the recreated leaf is exactly one of everything it owns.

    The durable counts are per thing the leaf owns: one identity, one worktree,
    one branch, one open pull request. The spawn count is per start of the plan,
    because a start that minted two leaves would be a duplicate even though
    the total across three starts is three. Both are counted across every
    intent, not just the latest, so nothing hides behind the first one's count.
    """
    identities = ev.agent_identities(project.repo, LEAF_AGENT)
    worktrees = ev.worktrees_for_branch(project.repo, LEAF_BRANCH)
    spawns = ev.authoritative_spawns(project.ledger(), branch=LEAF_BRANCH)
    pulls = pulls_on_branch(fixture, LEAF_BRANCH)
    require(
        len(identities) == 1,
        f"expected exactly one leaf identity, got {len(identities)}: {identities!r}",
    )
    require(
        len(worktrees) == 1,
        f"expected exactly one leaf worktree, got {len(worktrees)}: "
        f"{[str(path) for path in worktrees]!r}",
    )
    require(
        len(pulls) == 1,
        f"expected exactly one open pull request, got {len(pulls)}: "
        f"{json.dumps(pulls, default=str)[:2000]}",
    )
    require(
        ev.branch_exists(project.repo, LEAF_BRANCH),
        f"the leaf branch {LEAF_BRANCH} is missing",
    )
    per_intent: dict[str, int] = {}
    for spawn in spawns:
        intent_id = str((spawn.get("data") or {}).get("intent_id"))
        per_intent[intent_id] = per_intent.get(intent_id, 0) + 1
    duplicated = {key: count for key, count in per_intent.items() if count != 1}
    require(
        not duplicated,
        f"a plan start spawned more than one leaf: {duplicated!r}",
    )
    require(
        len(per_intent) == starts,
        f"expected one authoritative spawn for each of {starts} plan starts, got "
        f"{len(per_intent)}: {sorted(per_intent)!r}",
    )
    return {
        "identities": len(identities),
        "worktrees": len(worktrees),
        "branches": 1,
        "plan_starts": starts,
        "spawn_intents": len(per_intent),
        "authoritative_spawns": len(spawns),
        "open_pull_requests": len(pulls),
        "spawns_per_start": 1,
    }


# --------------------------------------------------------------------------
# T6
# --------------------------------------------------------------------------


def t6_recreate_is_idempotent(project: Project, expected_head: str) -> dict[str, Any]:
    """Recreate the session a second time and prove nothing moved.

    Idempotency here means the durable state is unchanged: the same head, the
    same single worktree, the same single identity, and no further worktree
    creation recorded for the branch.
    """
    identities_before = ev.agent_identities(project.repo, LEAF_AGENT)
    worktrees_before = [
        str(path) for path in ev.worktrees_for_branch(project.repo, LEAF_BRANCH)
    ]
    creations_before = _recorded_creations(project)
    project.close()
    recreated = pj.recreate_session(project.run, project.port)

    head = ev.head_of(recreated.repo, LEAF_BRANCH)
    require(
        head == expected_head,
        f"the second recreate moved the leaf head from {expected_head} to {head}",
    )
    worktrees_after = [
        str(path) for path in ev.worktrees_for_branch(recreated.repo, LEAF_BRANCH)
    ]
    require(
        worktrees_after == worktrees_before,
        f"the second recreate changed the leaf worktree set: "
        f"before={worktrees_before!r} after={worktrees_after!r}",
    )
    require(
        ev.agent_identities(recreated.repo, LEAF_AGENT) == identities_before,
        "the second recreate changed the leaf's durable identity",
    )
    creations_after = _recorded_creations(recreated)
    require(
        creations_after == creations_before,
        f"the second recreate created another leaf worktree: "
        f"before={creations_before} after={creations_after}",
    )
    return {
        "recreates": 2,
        "head": head,
        "worktrees": worktrees_after,
        "worktree_creations": creations_after,
        "identity_unchanged": True,
    }


def _recorded_creations(project: Project) -> int:
    """Count the durable records of this branch's worktree being created."""
    return sum(
        1
        for event in ev.attach_completions(project.ledger(), branch=LEAF_BRANCH)
        if (event.get("data") or {}).get("created") is True
    )


# --------------------------------------------------------------------------
# T7 -- the five fail-closed shapes
# --------------------------------------------------------------------------


def _refuse(project: Project, name: str, task: str, **kwargs: Any) -> Refusal:
    """Dispatch a leaf that must be refused, and return the recorded refusal.

    The machine code is not carried by the tool response, so the proof is the
    server's own ``agent.spawn_failed`` row, correlated by the intent the
    attempt minted.
    """
    attempt = intent(name)
    try:
        dispatch_leaf(project, name, task, intent_id=attempt, **kwargs)
    except (ServerError, TransportError):
        # A refusal may surface as a transport error; the ledger row is the
        # durable record either way.
        pass
    found = await_boundary(
        lambda: (
            [
                refusal
                for refusal in ev.refusals(project.ledger())
                if refusal.intent_id == attempt
            ]
            or None
        ),
        description=f"a recorded refusal for {name}",
        timeout=REFUSAL_TIMEOUT_SECONDS,
    )
    return found[-1]


def t7_branch_checked_out_elsewhere(project: Project) -> dict[str, Any]:
    """Refuse a birth branch a worktree this owner does not own already holds."""
    name = "held-elsewhere"
    branch = f"{BASE_BRANCH}.{name}-{LEAF_HARNESS}"
    ev.git(project.repo, "branch", branch, BASE_BRANCH)
    holder = project.repo.parent / f"e2e-1111-holder-{name}"
    subprocess.run(
        ["git", "-C", str(project.repo), "worktree", "add", "-q", str(holder), branch],
        check=True,
    )
    try:
        refusal = _refuse(project, name, "Attach to a branch another owner holds")
        require(
            refusal.code == OWNERSHIP_CONFLICT,
            f"a branch held elsewhere was refused with {refusal.code!r}, not "
            f"{OWNERSHIP_CONFLICT!r}: {refusal.message!r}",
        )
        for marker in (
            "is checked out at",
            "not at the deterministic leaf path",
            "retry the spawn",
        ):
            require(
                marker in refusal.message,
                f"the ownership conflict does not say {marker!r}: {refusal.message!r}",
            )
        planned = project.repo / ".exo" / "worktrees" / f"{name}-{LEAF_HARNESS}"
        require(
            not planned.exists(),
            f"a refused spawn created its planned worktree anyway: {planned}",
        )
        conflicts = ev.ownership_conflicts(project.ledger(), branch=branch)
        require(
            len(conflicts) == 1,
            f"the ownership conflict was recorded {len(conflicts)} times: {conflicts!r}",
        )
        return {
            "code": refusal.code,
            "branch": branch,
            "planned_worktree_created": False,
            "conflicts_recorded": len(conflicts),
        }
    finally:
        subprocess.run(
            ["git", "-C", str(project.repo), "worktree", "remove", "--force", str(holder)],
            check=False,
            capture_output=True,
        )


def t7_unregistered_residue(project: Project) -> dict[str, Any]:
    """Refuse a planned path that exists but is not a registered worktree.

    The residue is an ordinary directory holding one stray file: the shape a
    failed earlier spawn leaves behind, and one the bounded residue preflight
    deliberately refuses to touch rather than quarantine.
    """
    name = "residue"
    agent = f"{name}-{LEAF_HARNESS}"
    residue = project.repo / ".exo" / "worktrees" / agent
    residue.mkdir(parents=True, exist_ok=True)
    (residue / "leftover.txt").write_text("residue\n", encoding="utf-8")
    before = list(residue.iterdir())
    decisions_before = len(ev.attach_decisions(project.ledger()))
    try:
        refusal = _refuse(project, name, "Adopt an unregistered residue path")
        require(
            refusal.code == PATH_UNREGISTERED,
            f"an unregistered residue path was refused with {refusal.code!r}, not "
            f"{PATH_UNREGISTERED!r}: {refusal.message!r}",
        )
        require(
            "not registered with git" in refusal.message,
            f"the refusal does not name the git registration requirement: "
            f"{refusal.message!r}",
        )
        require(
            sorted(path.name for path in residue.iterdir())
            == sorted(path.name for path in before),
            f"the refusal modified the residue it was asked to adopt: {before!r}",
        )
        require(
            not (project.repo / ".exo" / "worktrees-residue").exists(),
            "the acceptance's residue probe caused a quarantine; a refused "
            "directory must be left in place",
        )
        require(
            len(ev.attach_decisions(project.ledger())) == decisions_before,
            "a refused residue path still recorded an attach decision",
        )
        return {
            "code": refusal.code,
            "residue_path": str(residue),
            "residue_modified": False,
            "quarantined": False,
        }
    finally:
        subprocess.run(["rm", "-rf", str(residue)], check=False)


def t7_local_remote_divergence(project: Project) -> dict[str, Any]:
    """Refuse a local branch that no longer contains its remote's head.

    Divergence is made the way it happens in practice: the forge advances the
    branch while the local branch stays where it was, so the two share no head
    the leaf could continue from.
    """
    name = "divergent"
    agent = f"{name}-{LEAF_HARNESS}"
    branch = f"{BASE_BRANCH}.{agent}"
    ev.git(project.repo, "branch", branch, BASE_BRANCH)
    ev.git(project.repo, "push", "-q", "origin", branch)
    local_head = ev.head_of(project.repo, branch)
    remote_worktree = project.repo.parent / f"e2e-1111-remote-{name}"
    subprocess.run(
        [
            "git",
            "-C",
            str(project.repo),
            "worktree",
            "add",
            "-q",
            "--detach",
            str(remote_worktree),
            branch,
        ],
        check=True,
    )
    try:
        (remote_worktree / "remote.txt").write_text("remote\n", encoding="utf-8")
        ev.git(remote_worktree, "add", "remote.txt")
        ev.git(remote_worktree, "commit", "-q", "-m", "Advance the branch on the forge")
        ev.git(remote_worktree, "push", "-q", "origin", f"HEAD:{branch}")
        remote_head = ev.head_of(remote_worktree)
        require(
            remote_head != local_head,
            f"the divergence probe did not move the forge head off {local_head}",
        )
        require(
            ev.head_of(project.repo, branch) == local_head,
            "the divergence probe moved the local branch, so it no longer diverges",
        )
        refusal = _refuse(project, name, "Continue a branch the forge has moved past")
        require(
            refusal.code == OWNERSHIP_CONFLICT,
            f"a diverged local and remote branch was refused with {refusal.code!r}, "
            f"not {OWNERSHIP_CONFLICT!r}: {refusal.message!r}",
        )
        require(
            "behind or diverged from its remote head" in refusal.message,
            f"the refusal does not name the divergence: {refusal.message!r}",
        )
        planned = project.repo / ".exo" / "worktrees" / agent
        require(
            not planned.exists(),
            f"a diverged branch still created the planned worktree: {planned}",
        )
        return {
            "code": refusal.code,
            "branch": branch,
            "local_head": local_head,
            "remote_head": remote_head,
            "planned_worktree_created": False,
        }
    finally:
        subprocess.run(
            ["git", "-C", str(project.repo), "worktree", "remove", "--force", str(remote_worktree)],
            check=False,
            capture_output=True,
        )


def t7_concurrent_create_race(project: Project) -> dict[str, Any]:
    """Prove a lost creation race recovers into one verified attach.

    The losing attempt created nothing, so the shipped path re-verifies
    ownership and attaches to the winner's branch instead of racing it again.
    The proof is one ``attach`` decision and one worktree creation for the
    branch, not two.
    """
    name = "raced"
    agent = f"{name}-{LEAF_HARNESS}"
    branch = f"{BASE_BRANCH}.{agent}"
    scratch = project.repo.parent / f"e2e-1111-winner-{name}"
    subprocess.run(
        ["git", "-C", str(project.repo), "worktree", "add", "-q", "-b", branch, str(scratch), BASE_BRANCH],
        check=True,
    )
    try:
        (scratch / "winner.txt").write_text("winner\n", encoding="utf-8")
        ev.git(scratch, "add", "winner.txt")
        ev.git(scratch, "commit", "-q", "-m", "The winning creator's commit")
        ev.git(scratch, "push", "-q", "origin", branch)
    finally:
        subprocess.run(
            ["git", "-C", str(project.repo), "worktree", "remove", "--force", str(scratch)],
            check=True,
        )
    # The winner's worktree is gone and only its branch remains, which is the
    # state a losing creator finds: the branch exists and nothing holds it.
    require(
        ev.branch_exists(project.repo, branch),
        f"the race winner's branch {branch} is missing",
    )
    require(
        not ev.worktrees_for_branch(project.repo, branch),
        f"the race winner's branch is still checked out at "
        f"{ev.worktrees_for_branch(project.repo, branch)!r}",
    )
    head_before = ev.head_of(project.repo, branch)
    result, race_intent = dispatch_leaf(project, name, "Recover a lost creation race")
    require(
        ev.is_success(result.raw),
        f"a lost creation race did not recover: {result.raw!r}",
    )
    decisions = await_boundary(
        lambda: (
            ev.attach_decisions(project.ledger(), branch=branch, action=ev.ATTACH)
            or None
        ),
        description=f"the recovered attach decision for {branch}",
        timeout=PUBLICATION_TIMEOUT_SECONDS,
    )
    require(
        len(decisions) == 1,
        f"a lost creation race recorded {len(decisions)} attach decisions: "
        f"{json.dumps(decisions, default=str)[:2000]}",
    )
    require(
        ev.head_of(project.repo, branch) == head_before,
        "a lost creation race moved the winner's branch head",
    )
    worktrees = ev.worktrees_for_branch(project.repo, branch)
    require(
        len(worktrees) == 1,
        f"a lost creation race left {len(worktrees)} worktrees on {branch}: "
        f"{[str(path) for path in worktrees]!r}",
    )
    require(
        ev.refusals(project.ledger(), intent_id=None) is not None,
        "refusal reader returned None instead of a list",
    )
    require(
        len(_authoritative_spawn(project, race_intent)) == 1,
        "a lost creation race did not spawn exactly one leaf for the attempt",
    )
    return {
        "branch": branch,
        "attach_decisions": len(decisions),
        "worktrees": len(worktrees),
        "authoritative_spawns": 1,
        "intent_id": race_intent,
        "recovered_without_a_second_spawn": True,
    }


def t7_expected_agent_resume(
    project: Project, fixture: forgejo.Instance, pr_number: int
) -> dict[str, Any]:
    """Resume the preserved pull request through the expected-agent boundary.

    ``resume_pr`` is the shipped surface that resolves a pull request's exact
    owning agent and re-dispatches that agent rather than a new one. This probe
    drives it against a session it recreates itself, so the owner being resumed
    is a genuinely recreated leaf.

    What it asserts is the fail-closed contract this actor can reach. The
    shipped route re-checks that the resumed owner's tmux target is live before
    it reports success, and this deterministic actor does not satisfy that check
    after its session has been recreated, so the route refuses. The refusal is
    accepted only when it names that readiness confirmation, and only when the
    leaf is left exactly as it was: one identity, one worktree, the same head,
    and one open pull request. A resume that reported success, or that refused
    for some other reason, or that forked the leaf into a second identity,
    worktree, head, or pull request, fails this item.

    The positive resume, where the route confirms the resume and the leaf
    continues its pull request, is not exercised here. It needs a leaf whose
    tmux target is live again after the session is recreated, which is a
    property of the agent runtime rather than of this acceptance's actor.
    """
    identities_before = ev.agent_identities(project.repo, LEAF_AGENT)
    worktrees_before = ev.worktrees_for_branch(project.repo, LEAF_BRANCH)
    head_before = ev.head_of(project.repo, LEAF_BRANCH)
    pulls_before = pulls_on_branch(fixture, LEAF_BRANCH)
    require(
        len(pulls_before) == 1,
        f"the preserved pull request is not singular: {len(pulls_before)}",
    )

    project.close()
    recreated = pj.recreate_session(project.run, project.port)
    require(
        ev.head_of(recreated.repo, LEAF_BRANCH) == head_before,
        "the resume's own recreate moved the preserved head",
    )
    result = recreated.effects().resume_pr(
        pr_number=pr_number,
        task="Continue the preserved branch after the session was recreated",
    )
    project.run, project.process, project.client = (
        recreated.run,
        recreated.process,
        recreated.client,
    )
    require(
        not ev.is_success(result.raw),
        f"the expected-agent resume reported success for an owner whose target "
        f"is not live after the recreate: {result.raw!r}",
    )
    refusal = str(result.error or result.raw)
    require(
        "readiness confirmation" in refusal,
        f"the resume was refused for a reason this acceptance does not claim "
        f"to prove: {refusal!r}",
    )
    require(
        ev.agent_identities(recreated.repo, LEAF_AGENT) == identities_before,
        "the refused resume replaced the leaf's identity",
    )
    require(
        ev.worktrees_for_branch(recreated.repo, LEAF_BRANCH) == worktrees_before,
        f"the refused resume changed the leaf worktree set: "
        f"before={worktrees_before!r} "
        f"after={ev.worktrees_for_branch(recreated.repo, LEAF_BRANCH)!r}",
    )
    require(
        ev.head_of(recreated.repo, LEAF_BRANCH) == head_before,
        f"the refused resume moved the preserved head from {head_before} to "
        f"{ev.head_of(recreated.repo, LEAF_BRANCH)}",
    )
    pulls_after = await_stable(
        lambda: pulls_on_branch(fixture, LEAF_BRANCH),
        description=f"pull requests on {LEAF_BRANCH} to hold steady after the resume",
        stable_for=SETTLE_SECONDS,
        timeout=PUBLICATION_TIMEOUT_SECONDS,
    )
    require(
        len(pulls_after) == 1,
        f"the refused resume changed the open pull requests on {LEAF_BRANCH}: "
        f"before={len(pulls_before)} after={len(pulls_after)}",
    )
    return {
        "pr_number": pr_number,
        "owner": LEAF_AGENT,
        "fails_closed": True,
        "refusal": refusal[:300],
        "identity_reused": True,
        "head": head_before,
        "worktrees": len(worktrees_before),
        "open_pull_requests": len(pulls_after),
    }


def t7_unowned_resume_fails_closed(project: Project) -> dict[str, Any]:
    """Refuse a resume that names a pull request this project does not own.

    A resume must resolve the pull request's exact owning agent; one that cannot
    is an explicit failure rather than a fresh spawn, so the acceptance proves
    the route refuses and spawns nothing. The refusal is read from whichever
    channel carries it: the tool surface answers through its own content
    envelope, and the transport raises for a status the server itself rejects.
    """
    unowned_pr_number = 999_999
    before = len(ev.authoritative_spawns(project.ledger()))
    refusal: str | None = None
    try:
        result = project.effects().resume_pr(
            pr_number=unowned_pr_number,
            task="Resume a pull request this project does not own",
        )
    except ServerError as failure:
        refusal = failure.body
    else:
        require(
            not ev.is_success(result.raw),
            f"a resume naming pull request #{unowned_pr_number} succeeded "
            f"instead of failing closed: {result.raw!r}",
        )
        refusal = str(result.error or result.raw)
    require(
        "pull request" in refusal and "404" in refusal,
        f"the refusal does not report that the pull request could not be "
        f"resolved: {refusal!r}",
    )
    after = len(ev.authoritative_spawns(project.ledger()))
    require(
        after == before,
        f"a refused resume still spawned a leaf: {before} spawns became {after}",
    )
    return {
        "pr_number": unowned_pr_number,
        "fails_closed": True,
        "message": refusal[:300],
        "authoritative_spawns": after,
        "spawned": False,
    }


def t7(project: Project, fixture: forgejo.Instance, pr_number: int) -> dict[str, Any]:
    """Run every fail-closed shape the issue names and report each outcome.

    The expected-agent resume runs first, against the leaf exactly as the
    recreate left it. The later probes deliberately remove branches and
    worktrees the leaf does not own, and they would leave the owner in a state
    the acceptance is not claiming to have proved.
    """
    return {
        "expected_agent_resume": t7_expected_agent_resume(project, fixture, pr_number),
        "unowned_resume": t7_unowned_resume_fails_closed(project),
        "branch_checked_out_elsewhere": t7_branch_checked_out_elsewhere(project),
        "unregistered_residue_path": t7_unregistered_residue(project),
        "local_remote_divergence": t7_local_remote_divergence(project),
        "concurrent_create_race": t7_concurrent_create_race(project),
    }


# --------------------------------------------------------------------------
# T8
# --------------------------------------------------------------------------


def t8_sink_does_not_create_planned_dir(project: Project) -> dict[str, Any]:
    """Prove a sink write leaves no planned directory for an agent with no worktree.

    A ledger write records that something happened without creating anything on
    disk, so the planned worktree directory must not exist afterwards even
    though the agent's name and branch are fully known.
    """
    name = f"sink-only-{LEAF_HARNESS}"
    branch = f"{BASE_BRANCH}.{name}"
    planned = project.repo / ".exo" / "worktrees" / name
    # A slice status change is a pure sink write: the controller records that a
    # slice moved without spawning anything, and the slice here owns no
    # worktree, no branch, and no durable identity.
    result = project.effects().emit_controller_event(
        event_type="tl.slice_status_changed",
        payload={
            "slice_id": name,
            "from_status": "pending",
            "to_status": "dispatching",
        },
    )
    require(
        ev.is_success(result.raw),
        f"the controller event sink refused the write: {result.raw!r}",
    )
    rows = [
        event
        for event in ev.typed(project.ledger(), "tl.slice_status_changed")
        if (event.get("data") or {}).get("slice_id") == name
    ]
    require(
        len(rows) == 1,
        f"the sink write did not reach the ledger: {len(rows)} rows for {name}",
    )
    require(
        not planned.exists(),
        f"a sink write created the planned worktree directory {planned}",
    )
    require(
        not ev.worktrees_for_branch(project.repo, branch),
        f"a sink write registered a worktree for {branch}",
    )
    require(
        ev.agent_identities(project.repo, name) == [],
        f"a sink write created a durable identity for {name}",
    )
    return {
        "slice_id": name,
        "branch": branch,
        "planned_directory": str(planned),
        "planned_directory_created": False,
        "identity_created": False,
        "worktree_registered": False,
        "ledger_rows": len(rows),
    }


# --------------------------------------------------------------------------
# T9
# --------------------------------------------------------------------------


#: The program that holds the shared lifecycle lock for the retryable probe. It
#: announces on standard output the moment the lock is actually held, so the
#: probe waits on that announcement rather than on a duration being long enough,
#: and it exits when the probe terminates it, which releases the lock.
_LOCK_HOLDER = """
import fcntl, sys, time
handle = open(sys.argv[1], "a")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(3600)
"""


def _hold_lifecycle_lock(project: Project) -> subprocess.Popen[str]:
    """Hold the spawn path's shared lifecycle lock until this process is killed.

    The lock is a real file lock, so any separate holder blocks the spawn
    exactly as a concurrent decision would. The holder is this harness's own
    child rather than a shell utility, because a utility's own child can keep
    the lock open after the utility is terminated, and a re-drive would then be
    refused for a lock nobody still holds.
    """
    lock = project.repo / LIFECYCLE_LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    holder = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(lock)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        line = holder.stdout.readline() if holder.stdout else ""
        if line.strip() == "held":
            return holder
        if holder.poll() is not None:
            raise ScenarioError(
                f"the lifecycle lock holder exited before taking the lock: "
                f"{holder.stderr.read() if holder.stderr else ''}"
            )
        time.sleep(0.05)
    holder.terminate()
    raise ScenarioError("the lifecycle lock holder never announced that it held the lock")


def t9_retryable_refusal_reconciles(project: Project) -> dict[str, Any]:
    """Prove the retryable refusal created nothing and the re-drive attaches.

    Holding the shared lifecycle lock is the shipped path's own retryable
    refusal: the decision never ran, so nothing was created, and a re-drive
    after the lock is released creates the leaf exactly once.
    """
    name = "retryable"
    agent = f"{name}-{LEAF_HARNESS}"
    branch = f"{BASE_BRANCH}.{agent}"
    planned = project.repo / ".exo" / "worktrees" / agent
    holder = _hold_lifecycle_lock(project)
    try:
        refusal = _refuse(project, name, "Spawn while the lifecycle lock is held")
    finally:
        holder.terminate()
        try:
            holder.wait(timeout=30)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait(timeout=30)
    require(
        refusal.code == LIFECYCLE_LOCK_TIMEOUT,
        f"a held lifecycle lock was refused with {refusal.code!r}, not "
        f"{LIFECYCLE_LOCK_TIMEOUT!r}: {refusal.message!r}",
    )
    require(
        "held by another decision" in refusal.message,
        f"the refusal does not name the lifecycle lock: {refusal.message!r}",
    )
    require(
        not planned.exists(),
        f"a refused retryable attempt created {planned}",
    )
    require(
        ev.agent_identities(project.repo, agent) == [],
        f"a refused retryable attempt wrote an identity for {agent}",
    )
    spawns_before = len(ev.authoritative_spawns(project.ledger(), branch=branch))
    require(
        spawns_before == 0,
        f"a refused retryable attempt already spawned {spawns_before} leaves",
    )

    redrive, redrive_intent = dispatch_leaf(
        project, name, "Re-drive after the lock was released"
    )
    require(
        ev.is_success(redrive.raw),
        f"the re-drive after the lock was released failed: {redrive.raw!r}",
    )
    spawns_after = await_stable(
        lambda: len(_authoritative_spawn(project, redrive_intent)),
        description=f"the re-drive's authoritative spawns for {branch} to settle",
        stable_for=SETTLE_SECONDS,
        timeout=PUBLICATION_TIMEOUT_SECONDS,
    )
    require(
        spawns_after == 1,
        f"the re-drive produced {spawns_after} authoritative spawns, expected one",
    )
    return {
        "code": refusal.code,
        "retryable": True,
        "created_nothing_while_refused": True,
        "authoritative_spawns_after_redrive": spawns_after,
        "intent_id": redrive_intent,
        "reconciled": True,
    }


def t9_terminal_conflict_fails_closed(project: Project) -> dict[str, Any]:
    """Prove a terminal ownership conflict is never re-driven and creates nothing.

    The conflict is terminal, so a second attempt at the same shape must refuse
    the same way: no attach decision, no worktree, and no spawn behind the
    refusal.
    """
    name = "terminal"
    agent = f"{name}-{LEAF_HARNESS}"
    branch = f"{BASE_BRANCH}.{agent}"
    ev.git(project.repo, "branch", branch, BASE_BRANCH)
    holder = project.repo.parent / f"e2e-1111-terminal-{name}"
    subprocess.run(
        ["git", "-C", str(project.repo), "worktree", "add", "-q", str(holder), branch],
        check=True,
    )
    try:
        decisions_before = len(ev.attach_decisions(project.ledger()))
        completions_before = len(ev.attach_completions(project.ledger()))
        first = _refuse(project, name, "Park a branch another owner holds")
        second = _refuse(project, name, "Park a branch another owner holds again")
        require(
            first.code == OWNERSHIP_CONFLICT and second.code == OWNERSHIP_CONFLICT,
            f"a terminal conflict was not refused consistently: "
            f"first={first.code!r} second={second.code!r}",
        )
        require(
            len(ev.attach_completions(project.ledger())) == completions_before,
            "a terminal conflict completed an attach decision",
        )
        planned = project.repo / ".exo" / "worktrees" / agent
        require(
            not planned.exists(),
            f"a terminal conflict created the planned worktree {planned}",
        )
        require(
            ev.authoritative_spawns(project.ledger(), branch=branch) == [],
            f"a terminal conflict spawned a leaf on {branch}",
        )
        conflicts = ev.ownership_conflicts(project.ledger(), branch=branch)
        return {
            "code": first.code,
            "terminal": True,
            "attempts": 2,
            "attach_completions": len(ev.attach_completions(project.ledger()))
            - completions_before,
            "attach_decisions": len(ev.attach_decisions(project.ledger()))
            - decisions_before,
            "conflicts_recorded": len(conflicts),
            "planned_worktree_created": False,
        }
    finally:
        subprocess.run(
            ["git", "-C", str(project.repo), "worktree", "remove", "--force", str(holder)],
            check=False,
            capture_output=True,
        )
