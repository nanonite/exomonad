"""Contract checks for the #1111 recreated-leaf acceptance.

These run without a server, a Forgejo, or a project, so they are the fast gate
that catches a broken harness before a real run is attempted. They cover three
things:

* the durable readers, driven against recorded artifacts, so a change in what
  the server writes is caught here rather than by a slow acceptance run
* the wiring: the T-items, the codes the acceptance expects, and the properties
  the compose template must keep
* the cleanup contract, by really starting a compose project, a tmux session,
  and a process through the run scope, then tearing it down and proving
  nothing it created is still there
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

HARNESS = Path(__file__).resolve().parent
PROJECT_ROOT = HARNESS.parents[2]
LIB_DIR = PROJECT_ROOT / "tests" / "e2e" / "lib"
sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(LIB_DIR))

import e2e_harness.cleanup as cl  # noqa: E402
import e2e_harness.tmuxio as tmuxio  # noqa: E402
import evidence as ev  # noqa: E402
import project as pj  # noqa: E402
import scenarios as sc  # noqa: E402
import e2e_harness.waiter as waiter  # noqa: E402
from run_prefix import PREFIX  # noqa: E402

#: One recorded ledger segment, written exactly as the server writes it: a
#: JSON object per line, carrying the envelope fields the readers must not care
#: about alongside the payload the acceptance asserts on.
RECORDED_EVENTS: list[dict[str, object]] = [
    {
        "schema_version": 1,
        "id": "a",
        "run_seq": 4,
        "type": "agent.attach_decided",
        "agent_id": "leaf-codex",
        "data": {
            "branch": "main.leaf-codex",
            "worktree_path": "/p/.exo/worktrees/leaf-codex",
            "action": "create_from_base",
            "branch_exists": False,
            "start_point": None,
        },
    },
    {
        "schema_version": 1,
        "id": "b",
        "run_seq": 5,
        "type": "agent.attach_completed",
        "agent_id": "leaf-codex",
        "data": {
            "branch": "main.leaf-codex",
            "worktree_path": "/p/.exo/worktrees/leaf-codex",
            "action": "create_from_base",
            "created": True,
        },
    },
    {
        "schema_version": 1,
        "id": "c",
        "run_seq": 7,
        "type": "agent.spawned",
        "agent_id": "parent",
        "data": {
            "child_agent": "leaf-codex",
            "agent_type": "Codex",
            "spawn_type": "leaf_subtree",
            "branch": "main.leaf-codex",
            "intent_id": "intent-1",
        },
    },
    {
        "schema_version": 1,
        "id": "d",
        "run_seq": 9,
        "type": "agent.spawned",
        "agent_id": "leaf-codex",
        "data": {"agent_type": "auto", "intent_id": "intent-1", "slug": "leaf"},
    },
    {
        "schema_version": 1,
        "id": "e",
        "run_seq": 11,
        "type": "agent.attach_decided",
        "agent_id": "leaf-codex",
        "data": {
            "branch": "main.leaf-codex",
            "worktree_path": "/p/.exo/worktrees/leaf-codex",
            "action": "attach",
            "branch_exists": True,
            "start_point": None,
        },
    },
    {
        "schema_version": 1,
        "id": "f",
        "run_seq": 13,
        "type": "agent.attach_completed",
        "agent_id": "leaf-codex",
        "data": {
            "branch": "main.leaf-codex",
            "worktree_path": "/p/.exo/worktrees/leaf-codex",
            "action": "attach",
            "created": True,
        },
    },
    {
        "schema_version": 1,
        "id": "g",
        "run_seq": 17,
        "type": "agent.branch_ownership_conflict",
        "agent_id": "leaf-codex",
        "data": {
            "branch": "main.held-codex",
            "worktree_path": "/p/.exo/worktrees/held-codex",
            "machine_code": "worktree.branch_ownership_conflict",
        },
    },
    {
        "schema_version": 1,
        "id": "h",
        "run_seq": 19,
        "type": "agent.spawn_failed",
        "agent_id": "parent",
        "data": {
            "child_agent": "held",
            "error": (
                "[worktree.branch_ownership_conflict] branch main.held-codex is "
                "checked out at /holder, not at the deterministic leaf path "
                "/p/.exo/worktrees/held-codex. Stop the agent holding "
                "main.held-codex or remove that worktree, then retry the spawn."
            ),
            "code": "worktree.branch_ownership_conflict",
            "source": "rust",
            "intent_id": "intent-2",
        },
    },
    {
        "schema_version": 1,
        "id": "i",
        "run_seq": 21,
        "type": "agent.spawn_failed",
        "agent_id": "parent",
        "data": {
            "child_agent": "residue",
            "error": "[worktree.path_unregistered] worktree path is not registered with git: /p/.exo/worktrees/residue-codex",
            "code": "worktree.path_unregistered",
            "source": "rust",
            "intent_id": "intent-3",
        },
    },
    {
        "schema_version": 1,
        "id": "j",
        "run_seq": 23,
        "type": "agent.spawn_failed",
        "agent_id": "parent",
        "data": {
            "child_agent": "retryable",
            "error": "[worktree.lifecycle_lock_timeout] worktree lifecycle lock is held by another decision; refusing to create or reuse a leaf worktree",
            "code": "worktree.lifecycle_lock_timeout",
            "source": "rust",
            "intent_id": "intent-4",
        },
    },
    {
        "schema_version": 1,
        "id": "k",
        "run_seq": 25,
        "type": "pr.filed",
        "agent_id": "leaf-codex",
        "data": {
            "pr_number": 7,
            "head_branch": "main.leaf-codex",
            "base_branch": "main",
            "head_sha": "a" * 40,
            "created": True,
        },
    },
    {
        "schema_version": 1,
        "id": "l",
        "run_seq": 27,
        "type": "agent.resumed",
        "agent_id": "parent",
        "data": {"pr_number": 7, "child_agent": "leaf-codex"},
    },
]


# --------------------------------------------------------------------------
# The durable readers
# --------------------------------------------------------------------------


def test_only_the_authoritative_spawn_counts():
    """The WASM log path writes a second ``agent.spawned`` with no branch."""
    assert len(ev.authoritative_spawns(RECORDED_EVENTS)) == 1


def test_authoritative_spawns_can_be_correlated_by_intent():
    assert (
        len(ev.authoritative_spawns(RECORDED_EVENTS, intent_id="intent-1")) == 1
    )
    assert ev.authoritative_spawns(RECORDED_EVENTS, intent_id="other") == []


def test_attach_decisions_are_readable_by_action_and_branch():
    assert len(ev.attach_decisions(RECORDED_EVENTS)) == 2
    attached = ev.attach_decisions(
        RECORDED_EVENTS, branch="main.leaf-codex", action=ev.ATTACH
    )
    assert len(attached) == 1
    assert attached[0]["data"]["branch_exists"] is True


def test_attach_completions_report_whether_the_worktree_was_created():
    completions = ev.attach_completions(RECORDED_EVENTS, branch="main.leaf-codex")
    assert [event["data"]["created"] for event in completions] == [True, True]


def test_a_refusal_exposes_its_code_and_its_prose_without_the_code_prefix():
    refusal = ev.refusals(RECORDED_EVENTS, intent_id="intent-2")[0]
    assert refusal.code == "worktree.branch_ownership_conflict"
    assert refusal.message.startswith("branch main.held-codex is checked out at")
    assert "[" not in refusal.message
    assert refusal.error.startswith("[worktree.branch_ownership_conflict]")
    assert refusal.child_agent == "held"


def test_every_refusal_code_the_acceptance_reads_is_a_real_code():
    codes = set(ev.refusal_codes(RECORDED_EVENTS))
    assert codes == {
        "worktree.branch_ownership_conflict",
        "worktree.path_unregistered",
        "worktree.lifecycle_lock_timeout",
    }


def test_ownership_conflicts_are_readable_per_branch():
    assert len(ev.ownership_conflicts(RECORDED_EVENTS)) == 1
    assert (
        len(
            ev.ownership_conflicts(
                RECORDED_EVENTS, branch="main.held-codex"
            )
        )
        == 1
    )
    assert ev.ownership_conflicts(RECORDED_EVENTS, branch="main.other") == []


def test_publications_are_found_under_either_branch_field():
    assert len(ev.publications(RECORDED_EVENTS)) == 1
    assert len(ev.publications(RECORDED_EVENTS, branch="main.leaf-codex")) == 1
    assert ev.publications(RECORDED_EVENTS, branch="main.other") == []


def test_typed_ignores_records_without_an_object_payload():
    assert ev.typed([{"type": "agent.spawned", "data": "text"}], "agent.spawned") == []


def test_ledger_events_reads_every_segment_in_order(tmp_path: Path):
    segments = tmp_path / ".exo" / "ledger" / "segments"
    segments.mkdir(parents=True)
    (segments / "segment-000000000000.jsonl").write_text(
        json.dumps(RECORDED_EVENTS[0]) + "\n",
        encoding="utf-8",
    )
    (segments / "segment-000000000001.jsonl").write_text(
        "\n".join(json.dumps(event) for event in RECORDED_EVENTS[1:3]) + "\n",
        encoding="utf-8",
    )
    assert [event["id"] for event in ev.ledger_events(tmp_path)] == ["a", "b", "c"]


def test_ledger_events_report_a_project_with_no_ledger(tmp_path: Path):
    with pytest.raises(ev.EvidenceError):
        ev.ledger_events(tmp_path)


def test_published_heads_accept_both_document_shapes(tmp_path: Path):
    agent_dir = tmp_path / ".exo"
    agent_dir.mkdir(parents=True)
    (agent_dir / "published-heads.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "heads": [{"pr_number": 7, "head_branch": "main.leaf-codex"}],
            }
        ),
        encoding="utf-8",
    )
    assert ev.published_heads(tmp_path) == [
        {"pr_number": 7, "head_branch": "main.leaf-codex"}
    ]
    assert ev.published_heads(tmp_path.parent / "absent") == []


def test_pull_number_is_found_through_the_tool_envelope():
    payload = {"success": True, "result": {"content": [{"text": '{"pr_number": 7}'}]}}
    assert ev.find_pull_number(payload) == 7
    assert ev.find_pull_number({"success": True}) is None


def test_success_is_read_through_the_tool_envelope():
    assert ev.is_success({"success": True}) is True
    assert ev.is_success({"content": '{"success": true}'}) is True
    assert ev.is_success({"success": False}) is False


# --------------------------------------------------------------------------
# The waits
# --------------------------------------------------------------------------


def test_a_wait_returns_the_first_non_empty_probe():
    values = iter([None, None, "reached"])
    assert (
        waiter.await_boundary(
            lambda: next(values, None),
            description="the boundary",
            timeout=5.0,
            poll_interval=0.01,
        )
        == "reached"
    )


def test_a_wait_reports_what_it_last_saw():
    with pytest.raises(waiter.Timeout) as failure:
        waiter.await_boundary(
            lambda: None,
            description="the boundary",
            timeout=0.2,
            poll_interval=0.01,
        )
    assert "the boundary" in str(failure.value)
    assert "last observed state" in str(failure.value)


def test_a_wait_needs_no_elapsed_time_to_succeed():
    started = time.monotonic()
    assert (
        waiter.await_boundary(
            lambda: "already there",
            description="the boundary",
            timeout=30.0,
            poll_interval=0.01,
        )
        == "already there"
    )
    assert time.monotonic() - started < 5.0


def test_there_is_no_quiescence_wait_to_misuse():
    """A count that stopped moving is not evidence, so no such wait exists.

    Every count in this acceptance is read after the agent's own record that
    its invocation finished. If a wait-for-quiescence helper ever comes back, a
    duplicate could land after its window and pass silently.
    """
    assert not hasattr(waiter, "await_stable")
    assert not hasattr(waiter, "await_boundary_stable")


# --------------------------------------------------------------------------
# The session name the server and the harness must agree on
# --------------------------------------------------------------------------


def test_the_server_still_rejects_a_session_name_it_cannot_use():
    """The harness names a session the server must accept verbatim.

    The harness creates the tmux session itself rather than through
    ``exomonad init``, so it holds its own name. The server resolves
    ``tmux_session`` from the config and now fails closed on a name over the
    limit instead of truncating it — a truncated name left the server probing a
    session nobody created, and every agent was reported dead. The harness no
    longer has to mirror a rewrite, but it still has to stay inside the limit.
    """
    source = (PROJECT_ROOT / "rust/exomonad/src/config.rs").read_text(encoding="utf-8")
    assert "chars().take(36)" not in source, (
        "the server is truncating the session name again; the harness relies on "
        "config load rejecting an over-long name instead"
    )
    assert "pub const TMUX_SESSION_MAX_CHARS: usize = 36;" in source, (
        "the session-name limit is no longer a named constant the harness can read"
    )
    assert "SessionNameTooLong" in source, (
        "config load no longer reports a typed error naming the over-long value"
    )


def test_every_session_name_the_harness_creates_fits_the_servers_limit():
    """Every name this harness produces is one the server will accept.

    The server rejects a name over the limit at config load, so a harness name
    that crossed it would stop the run at startup instead of misnaming a
    session.
    """
    import driver

    for _attempt in range(50):
        run_id = driver.run_id()
        scope = cl.RunScope(run_id=run_id, root=cl.make_root("/tmp", PREFIX), prefix=PREFIX)
        try:
            name = pj.session_name(scope)
            assert "." not in name, f"the server rewrites dots in {name!r}"
            assert len(name) <= cl.SESSION_NAME_MAX_LENGTH
        finally:
            shutil.rmtree(scope.root, ignore_errors=True)


def test_a_run_id_that_would_overflow_the_session_name_is_refused():
    """An over-long run id fails immediately instead of silently misnaming.

    The failure has to be loud. A name the server truncates produces a session
    the server cannot find, and every later liveness check reports every agent
    as dead, which reads as a product fault rather than as a naming mistake.
    """
    scope = cl.RunScope(
            run_id="x" * 40, root=cl.make_root("/tmp", PREFIX), prefix=PREFIX
        )
    try:
        with pytest.raises(cl.CleanupError) as failure:
            _ = scope.session_prefix
        assert "session name limit" in str(failure.value)
    finally:
        shutil.rmtree(scope.root, ignore_errors=True)


def test_a_session_name_over_the_limit_is_refused_rather_than_tracked():
    scope = cl.RunScope(
            run_id="e2e1111-abc123",
            root=cl.make_root("/tmp", PREFIX),
            prefix=PREFIX,
        )
    try:
        with pytest.raises(cl.CleanupError) as failure:
            scope.track_session(scope.session_prefix + "a" * 40)
        assert "session name limit" in str(failure.value)
    finally:
        shutil.rmtree(scope.root, ignore_errors=True)


# --------------------------------------------------------------------------
# The wiring
# --------------------------------------------------------------------------


def test_the_walk_declares_every_t_item_the_issue_names_in_order():
    """T1 through T9 must each be a step the walk actually runs."""
    import driver

    walk = driver.Walk.__new__(driver.Walk)
    walk.scope = None
    walk.instance = None
    walk.results = {}
    walk.evidence = {}
    walk.publication = {}
    walk.starts = 0
    steps = driver.Walk.steps(walk)
    assert driver.ITEMS == ("T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9")
    assert [name for name, _step in steps] == list(driver.ITEMS)
    assert all(callable(step) for _name, step in steps)


def test_the_acceptance_only_expects_codes_the_product_can_emit():
    declared = {
        sc.OWNERSHIP_CONFLICT,
        sc.PATH_UNREGISTERED,
        sc.LIFECYCLE_LOCK_TIMEOUT,
        sc.BRANCH_EXISTS,
    }
    for code in declared:
        assert code.startswith("worktree."), code


def test_the_retryable_codes_the_acceptance_proves_are_retryable_to_the_controller():
    """The acceptance's retryable code must be one the controller will retry.

    If the product ever stops classifying it as retryable, the acceptance's
    T9 would be proving a property the controller no longer relies on.
    """
    sys.path.insert(0, str(PROJECT_ROOT / "tl_loop"))
    from loop.dispatch_classification import DispatchFailureClass, classify_dispatch_failure

    assert (
        classify_dispatch_failure(sc.LIFECYCLE_LOCK_TIMEOUT)
        is DispatchFailureClass.RETRYABLE
    )
    assert (
        classify_dispatch_failure(sc.OWNERSHIP_CONFLICT)
        is DispatchFailureClass.TERMINAL
    )
    assert (
        classify_dispatch_failure(sc.PATH_UNREGISTERED)
        is DispatchFailureClass.TERMINAL
    )


def test_the_run_script_refuses_to_run_without_this_worktrees_build():
    script = (HARNESS / "run.sh").read_text(encoding="utf-8")
    assert "target/debug/exomonad" in script
    assert ".exo/wasm/wasm-guest-devswarm.wasm" in script
    assert "driver.py" in script
    assert "install-all" not in script


def test_the_compose_template_keeps_the_properties_a_disposable_instance_needs():
    raw = (PROJECT_ROOT / "tests/e2e/lib/forgejo/docker-compose.yml").read_text(
        encoding="utf-8"
    )
    # The properties are about the compose specification, so the file's own
    # prose about them is not the specification.
    specification = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("#")
    )
    # A fixed container name or host port would make two runs collide and would
    # let a previous run's container or database answer a later run.
    assert "container_name" not in specification
    assert '"127.0.0.1::3000"' in specification
    assert not any(
        line.strip().startswith("- ") and ":" in line and "::" not in line
        for line in specification.splitlines()
        if "127.0.0.1" in line
    ), "a fixed host port would make two runs collide"
    # The instance must be locked, unregistered, and without Actions.
    assert "FORGEJO__security__INSTALL_LOCK=true" in specification
    assert "FORGEJO__service__DISABLE_REGISTRATION=true" in specification
    assert "FORGEJO__actions__ENABLED=false" in specification
    # The volume must be named, so compose scopes it to the project.
    assert "forgejo-data:/data" in specification
    assert "volumes:" in specification


def test_the_run_owns_a_directory_named_by_mktemp_and_never_a_fixed_path():
    source = (
        LIB_DIR / "e2e_harness" / "cleanup.py"
    ).read_text(encoding="utf-8")
    assert '"mktemp", "-d"' in source


# --------------------------------------------------------------------------
# The cleanup contract
# --------------------------------------------------------------------------


def _docker_available() -> bool:
    return (
        subprocess.run(
            ["docker", "info"],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


@pytest.fixture
def scope() -> Any:
    """A uniquely named run scope that is torn down however the test ends.

    A test that starts a real session, process, or compose project must not be
    able to leave it running when an assertion fails: that is the leak class
    this section exists to catch, so the contract tests hold themselves to it.
    Each test gets its own run id, so two of them can never collide on a name.
    """
    import secrets

    created = cl.RunScope(
        run_id=f"ct{secrets.token_hex(3)}",
        root=cl.make_root("/tmp", PREFIX),
        prefix=PREFIX,
    )
    try:
        yield created
    finally:
        problems = created.teardown()
        assert problems == [], f"the contract test leaked: {problems}"


def _start_probe_session(scope: cl.RunScope) -> str:
    """Create the run's real tmux session.

    The name is the scope's own session name, with nothing appended: the prefix
    is already unique per test, and appending a label would push the name past
    the server's limit, which is the mistake this contract exists to prevent.
    """
    session = scope.track_session(scope.session_prefix)
    tmuxio.tmux(
        scope.tmux_socket,
        "new-session",
        "-d",
        "-s",
        session,
        "-n",
        "probe",
        "sleep",
        "600",
        check=True,
    )
    return session


@pytest.mark.skipif(not _tmux_available(), reason="tmux is required")
def test_teardown_removes_the_session_and_the_process_it_started(scope):
    """A real session and a real process, torn down, leave nothing behind.

    This is the leak class that previously went unnoticed: a harness that
    starts a session and a server and then fails an item leaves both running.
    """
    session = _start_probe_session(scope)
    process = scope.track_process(
        subprocess.Popen(["sleep", "600"], cwd=scope.root, start_new_session=True),
        "probe process",
    )
    assert cl._session_exists(scope.tmux_socket, session)
    assert cl._process_alive(process.pid)

    problems = scope.teardown()

    assert problems == []
    assert scope.leaks() == []
    assert not cl._session_exists(scope.tmux_socket, session)
    assert not cl._process_alive(process.pid)
    assert not scope.root.exists()


@pytest.mark.skipif(not _tmux_available(), reason="tmux is required")
def test_teardown_is_idempotent(scope):
    """Tearing down twice is not an error, because cleanup runs on every path."""
    _start_probe_session(scope)
    assert scope.teardown() == []
    assert scope.teardown() == []
    assert scope.leaks() == []


@pytest.mark.skipif(not _tmux_available(), reason="tmux is required")
def test_a_leaked_session_is_reported_rather_than_ignored(scope):
    """The scope fails the run when a session it owns is still there."""
    session = f"{scope.session_prefix}orphan"
    tmuxio.tmux(
        scope.tmux_socket,
        "new-session",
        "-d",
        "-s",
        session,
        "-n",
        "probe",
        "sleep",
        "600",
        check=True,
    )
    scope.sessions.add(session)
    found = cl._sessions_with_prefix(scope.tmux_socket, scope.session_prefix)
    assert found == [f"tmux session survived cleanup: {session}"]
    assert scope.leaks() != []
    with pytest.raises(cl.CleanupError) as failure:
        scope.require_clean()
    assert session in str(failure.value)


@pytest.mark.skipif(not _tmux_available(), reason="tmux is required")
def test_a_leaked_process_under_the_run_directory_is_reported(scope):
    """A process still running in the run's directory is found by its cwd."""
    process = subprocess.Popen(
        ["sleep", "600"], cwd=scope.root, start_new_session=True
    )
    try:
        found = cl._processes_under(scope.root)
        assert any(str(process.pid) in item for item in found), found
    finally:
        process.terminate()
        process.wait(timeout=30)


def test_the_scope_refuses_to_own_a_resource_it_did_not_name(scope):
    """A scope must not be able to tear down something outside its own prefix."""
    with pytest.raises(cl.CleanupError):
        scope.track_session("exo-workers")
    with pytest.raises(cl.CleanupError):
        scope.track_compose("some-other-project", Path("docker-compose.yml"))


@pytest.mark.skipif(
    not (_docker_available() and _tmux_available()),
    reason="docker and tmux are required",
)
def test_teardown_removes_a_compose_project_and_its_volume(scope):
    """A real compose project, torn down, leaves no project and no volume."""
    import e2e_harness.forgejo_stack as fj

    compose_file = fj.template_path(PROJECT_ROOT)
    project = scope.track_compose(f"{scope.session_prefix}compose", compose_file)
    fj.up(project, compose_file)
    host = fj.published_host(project, compose_file)
    assert host.count(":") == 1
    volume = f"{project}_forgejo-data"
    assert volume in _volumes()

    problems = scope.teardown()

    assert problems == [], problems
    assert scope.leaks() == []
    assert volume not in _volumes()
    assert not _compose_projects_present(project)


def _volumes() -> list[str]:
    result = subprocess.run(
        ["docker", "volume", "ls", "--format", "{{.Name}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.split()


def _compose_projects_present(project: str) -> bool:
    result = subprocess.run(
        ["docker", "compose", "ls", "--all", "--format", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return True
    return project in result.stdout


@pytest.mark.skipif(
    not (_docker_available() and _tmux_available()),
    reason="docker and tmux are required",
)
def test_a_compose_project_and_a_session_together_leave_nothing_behind(scope):
    """The shape the acceptance itself creates: a session plus a forge."""
    import e2e_harness.forgejo_stack as fj

    compose_file = fj.template_path(PROJECT_ROOT)
    project = scope.track_compose(f"{scope.session_prefix}both", compose_file)
    _start_probe_session(scope)
    fj.up(project, compose_file)
    fj.published_host(project, compose_file)
    scope.track_process(
        subprocess.Popen(["sleep", "600"], cwd=scope.root, start_new_session=True),
        "probe process",
    )

    # The detector must see what the run created before teardown; that is what
    # makes an empty result afterwards mean something.
    assert scope.leaks() != []
    assert scope.teardown() == []
    assert scope.leaks() == []
    scope.require_clean()


# --------------------------------------------------------------------------
# The interrupted-run contract
# --------------------------------------------------------------------------


@pytest.mark.skipif(not _tmux_available(), reason="tmux is required")
def test_a_sweep_reclaims_a_run_that_was_killed_before_its_teardown():
    """A run killed from outside is reclaimed by the next run's sweep.

    This is the leak a SIGKILL, a host reboot, or a Docker restart produces: the
    trap never ran, so the run's server is reparented, its session and compose
    project and directory are still there, and nothing that only consults the
    scope's own records can see any of it. The sweep is prefix-driven for
    exactly that reason.
    """
    import secrets

    prefix = f"{PREFIX}aborted-{secrets.token_hex(4)}-"
    root = cl.make_root("/tmp", PREFIX)
    process = subprocess.Popen(
        ["sleep", "600"], cwd=root, start_new_session=True
    )
    session = f"{prefix}session"
    tmuxio.tmux(
        tmuxio.socket_path(root),
        "new-session",
        "-d",
        "-s",
        session,
        "-n",
        "probe",
        "sleep",
        "600",
        check=True,
    )
    # No scope is created and no teardown is ever called: this run is aborted.
    try:
        assert cl.stale_run_roots(PREFIX) != []
        assert any(
            pid == str(process.pid)
            for pid, _cwd, _command in cl.processes_in_scopes([str(root)])
        )
        assert cl.sweep_stale(prefix, _compose_file(), PREFIX) == []
    finally:
        if cl._process_alive(process.pid):
            process.terminate()
            process.wait(timeout=30)
        tmuxio.tmux(tmuxio.socket_path(root), "kill-session", "-t", session)
        shutil.rmtree(root, ignore_errors=True)

    assert not cl._process_alive(process.pid)
    assert not cl._session_exists(tmuxio.socket_path(root), session)
    assert not root.exists()
    assert cl.processes_in_scopes([str(root)]) == []


def test_the_sweep_kills_a_server_before_it_removes_the_directory_it_ran_in():
    """Removing the directory is the last thing a sweep does, not the first.

    A process whose working directory has been deleted keeps running, and is
    then indistinguishable from a process that was never in a run directory at
    all. This asserts the order directly: the directory is gone when the sweep
    returns, and the process that was running in it is dead by then too.
    """
    import secrets

    prefix = f"{PREFIX}order-{secrets.token_hex(3)}-"
    root = cl.make_root("/tmp", PREFIX)
    process = subprocess.Popen(["sleep", "600"], cwd=root, start_new_session=True)
    try:
        assert cl.sweep_stale(prefix, _compose_file(), PREFIX) == []
        assert not root.exists(), "the sweep left the run directory behind"
        assert not cl._process_alive(process.pid), (
            "the sweep removed the directory but left the process running in it"
        )
    finally:
        if cl._process_alive(process.pid):
            process.kill()
            process.wait(timeout=30)
        shutil.rmtree(root, ignore_errors=True)


def test_a_process_that_merely_mentions_a_run_path_survives_the_sweep():
    """A run's processes are identified by where they run, not by what they say.

    An earlier version also matched a run path anywhere on a command line, so
    the start-of-run sweep matched any process *carrying* such a path as an
    argument -- an editor, a ``grep``, a ``tail``, or an agent whose own prompt
    quoted one -- and killed its process group along with the leak it was
    looking for. Two processes are started here that differ in exactly that
    respect, and only the one running inside the run directory may be killed.
    """
    import secrets

    prefix = f"{PREFIX}argv-{secrets.token_hex(3)}-"
    root = cl.make_root("/tmp", PREFIX)
    # Runs inside the run directory: this one is the run's own and must die.
    inside = subprocess.Popen(["sleep", "600"], cwd=root, start_new_session=True)
    # Runs elsewhere and merely names the run directory as an argument: this one
    # belongs to whoever is reading about the run, and must be left alone.
    elsewhere = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; print(sys.argv[1]); time.sleep(600)", str(root)],
        cwd="/",
        start_new_session=True,
        stdout=subprocess.DEVNULL,
    )
    try:
        assert any(
            pid == str(inside.pid)
            for pid, _cwd, _command in cl.processes_in_scopes([str(root)])
        )
        assert not any(
            pid == str(elsewhere.pid)
            for pid, _cwd, _command in cl.processes_in_scopes([str(root)])
        ), "a process that only mentions the run path was treated as the run's"
        assert cl.sweep_stale(prefix, _compose_file(), PREFIX) == []
        assert not cl._process_alive(inside.pid), "the sweep missed a run process"
        assert cl._process_alive(elsewhere.pid), (
            "the sweep killed a process that only mentioned the run path"
        )
    finally:
        for process in (inside, elsewhere):
            if cl._process_alive(process.pid):
                process.kill()
                process.wait(timeout=30)
        shutil.rmtree(root, ignore_errors=True)


def test_the_leak_check_sees_a_process_whose_directory_is_already_gone():
    """A survivor in a deleted run directory is still a survivor.

    The kernel reports a removed working directory with ``(deleted)`` appended,
    which is precisely the state a leaked server is left in once teardown has
    removed the directory. Without stripping that suffix the check reports clean
    while the process is still running.
    """
    root = cl.make_root("/tmp", PREFIX)
    process = subprocess.Popen(["sleep", "600"], cwd=root, start_new_session=True)
    try:
        shutil.rmtree(root, ignore_errors=True)
        reported = cl._live_processes()
        mine = [entry for entry in reported if entry[0] == str(process.pid)]
        assert mine, "the process did not report its removed working directory"
        assert mine[0][1] == str(root), mine[0][1]
        assert not mine[0][1].endswith(" (deleted)")
        found = cl._processes_under(root)
        assert any(str(process.pid) in item for item in found), found
    finally:
        process.terminate()
        process.wait(timeout=30)


def test_the_scope_tracks_a_process_that_runs_in_its_own_session():
    """A child in its own session is still the scope's to kill.

    ``start_new_session=True`` detaches a child into its own session and process
    group, which is what lets it be signalled without touching the harness. It
    also means nothing about parentage identifies it afterwards, so the scope has
    to hold the pid itself and the leak check has to find it by its directory.
    """
    scope = cl.RunScope(
        run_id=f"contract-{os.getpid()}",
        root=cl.make_root("/tmp", PREFIX),
        prefix=PREFIX,
    )
    process = scope.track_process(
        subprocess.Popen(["sleep", "600"], cwd=scope.root, start_new_session=True),
        "detached probe",
    )
    assert os.getpgid(process.pid) == process.pid, "the probe is not its own group"
    assert any(
        pid == str(process.pid)
        for pid, _cwd, _command in cl._live_processes()
    )
    assert scope.teardown() == []
    assert not cl._process_alive(process.pid)
    assert scope.leaks() == []


def _compose_file() -> Path:
    """Return the shared disposable Forgejo template."""
    return PROJECT_ROOT / "tests" / "e2e" / "lib" / "forgejo" / "docker-compose.yml"


# --------------------------------------------------------------------------
# The tmux boundary
# --------------------------------------------------------------------------


#: Harness modules that predate the shared wrapper. Each one creates its
#: sessions from its own shell entry point and has no run directory to name a
#: socket from, so converting them means changing their shell entry points too
#: -- a separate change from the shared package this contract guards. Anything
#: *not* in this set must go through ``e2e_harness.tmuxio``.
LEGACY_TMUX_CALLERS = frozenset(
    {
        "tests/e2e/init-continue/run.py",
        "tests/e2e/ordered-recursive/ordered_recursive.py",
        "tests/e2e/pre-pr-recovery-fsm/run.py",
        "tests/e2e/task-blocked-human-gate/run.py",
        "tests/e2e/tl-loop-active/active_run.py",
        "tests/e2e/tl-loop-shadow/shadow_companion.py",
    }
)

#: A tmux *invocation*: the binary as an argv element, or a shell command line
#: that starts with it. A bare ``tmux`` resolves its server from the inherited
#: ``TMUX`` variable first, so an invocation that does not pass ``-S`` talks to
#: whichever server owns the caller's pane.
_TMUX_ARGV = re.compile(r"""\[\s*["']tmux["']|[,\[]\s*["']tmux["']\s*,""")
_TMUX_SHELL = re.compile(
    r"""^\s*tmux\s+(?:-[A-Za-z]+\s+)*(?:new-session|kill-session|has-session|"""
    r"""kill-window|new-window|set-environment|list-sessions|list-windows|"""
    r"""list-panes|capture-pane|display-message|send-keys|kill-server)"""
)


def _tmux_bypasses(path: Path) -> list[int]:
    """Return the line numbers where a file invokes tmux outside the wrapper."""
    offenders: list[int] = []
    for number, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        if line.strip().startswith("#"):
            continue
        if _TMUX_ARGV.search(line) or _TMUX_SHELL.search(line):
            offenders.append(number)
    return offenders


#: The harness trees that share a run scope, and therefore a socket to name.
#: Their shell entry points are guarded too, because a ``run.sh`` that reaches
#: for a bare ``tmux`` is exactly how a session ends up on somebody else's
#: server.
SHARED_HARNESS_TREES = (
    "tests/e2e/lib/",
    "tests/e2e/recreated-leaf-recovery/",
    "tests/e2e/recursive-crash-convergence/",
    "tests/e2e/ordered-recursive/",
)


def _tmux_guard_targets() -> list[Path]:
    """Return every file the wrapper contract holds to account.

    Every Python driver under ``tests/e2e`` is in scope: a driver is the thing
    that owns a run, and the wrapper is a Python module. Shell scripts are in
    scope only inside the shared harness trees, where the shell entry point and
    the run scope are the same program. The harnesses outside those trees predate
    the shared package, create their sessions from their own shell, and are named
    individually in ``LEGACY_TMUX_CALLERS`` when they also drive tmux from
    Python.
    """
    targets: list[Path] = []
    for path in sorted((PROJECT_ROOT / "tests" / "e2e").rglob("*")):
        if path.suffix not in {".py", ".sh"} or path.name == "tmuxio.py":
            continue
        if path.suffix == ".py":
            targets.append(path)
            continue
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative.startswith(SHARED_HARNESS_TREES):
            targets.append(path)
    return targets


def test_no_harness_bypasses_the_shared_tmux_wrapper() -> None:
    """Nothing in scope may talk to tmux without naming its server.

    A bare ``tmux`` resolves the server from the inherited ``TMUX`` variable
    *before* it reads ``TMUX_TMPDIR`` or any ``-S`` flag, so a harness started
    from inside a pane silently addresses the server that owns the pane. That is
    how a harness whose docstring claimed the operator's sessions were
    unreachable ended up killing them. Every call therefore has to go through
    ``e2e_harness.tmuxio``, which names the run's own socket and strips ``TMUX``
    so nothing can override it.
    """
    found: dict[str, list[int]] = {}
    for path in _tmux_guard_targets():
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative in LEGACY_TMUX_CALLERS:
            continue
        lines = _tmux_bypasses(path)
        if lines:
            found[relative] = lines
    assert not found, (
        "tmux must only be invoked through e2e_harness.tmuxio; add -S with the "
        f"run's own socket instead: {found}"
    )


def test_the_legacy_tmux_exemptions_are_still_real() -> None:
    """The exemption list cannot rot: every entry still exists and still calls tmux.

    An allowlist that keeps entries after they were converted stops describing
    anything, so each one has to keep justifying itself.
    """
    for relative in sorted(LEGACY_TMUX_CALLERS):
        path = PROJECT_ROOT / relative
        assert path.is_file(), f"legacy tmux exemption no longer exists: {relative}"
        assert _tmux_bypasses(path), (
            f"legacy tmux exemption no longer calls tmux; drop it: {relative}"
        )


@pytest.mark.skipif(not _tmux_available(), reason="tmux is required")
def test_teardown_cannot_reach_a_tmux_server_outside_the_run() -> None:
    """A run started from inside a pane must not stop that pane's server.

    The outer server here stands in for the operator's: its socket is put in
    ``TMUX`` exactly as a pane would carry it, and the harness's own teardown
    and stale-run sweep are then run with that variable set. Both must touch
    only the run's own server, which is why the run's session is gone
    afterwards while the outer session is still there.
    """
    # Not ``tmp_path``: the outer server's socket has to fit in ``sun_path``,
    # and ``tmp_path`` descends from ``TMPDIR``, which a caller may have made
    # long enough that no socket path fits under it.
    outer_root = tmuxio.short_root()
    outer_socket = tmuxio.socket_path(outer_root)
    outer_session = "exo-e2e-outer-server-guard"
    tmuxio.tmux(
        outer_socket,
        "new-session",
        "-d",
        "-s",
        outer_session,
        "sleep",
        "600",
        check=True,
    )
    previous = os.environ.get("TMUX")
    stale_root = cl.make_root("/tmp", PREFIX)
    scope = cl.RunScope(
        run_id=f"guard{os.getpid()}", root=cl.make_root("/tmp", PREFIX), prefix=PREFIX
    )
    session = scope.track_session(scope.session_prefix)
    stale_session = f"{PREFIX}stale-guard"
    os.environ["TMUX"] = f"{outer_socket},{os.getpid()},0"
    try:
        tmuxio.tmux(
            scope.tmux_socket,
            "new-session",
            "-d",
            "-s",
            session,
            "-n",
            "probe",
            "sleep",
            "600",
            check=True,
        )
        tmuxio.tmux(
            tmuxio.socket_path(stale_root),
            "new-session",
            "-d",
            "-s",
            stale_session,
            "sleep",
            "600",
            check=True,
        )
        assert tmuxio.server_alive(outer_socket)
        assert tmuxio.server_alive(scope.tmux_socket)

        assert scope.teardown() == []
        assert cl.sweep_stale(PREFIX, _compose_file(), PREFIX) == []
        assert scope.leaks() == []

        # The outer server is untouched by both, and both of the run's servers
        # are gone.
        assert tmuxio.tmux(
            outer_socket, "has-session", "-t", outer_session
        ).returncode == 0, "the outer tmux server lost its session"
        assert not scope.tmux_socket.parent.is_dir()
        assert not tmuxio.server_alive(tmuxio.socket_path(stale_root))
    finally:
        if previous is None:
            os.environ.pop("TMUX", None)
        else:
            os.environ["TMUX"] = previous
        tmuxio.tmux(outer_socket, "kill-server")
        shutil.rmtree(outer_root, ignore_errors=True)
        shutil.rmtree(stale_root, ignore_errors=True)
        shutil.rmtree(scope.root, ignore_errors=True)


# --------------------------------------------------------------------------
# Socket paths and TMPDIR
# --------------------------------------------------------------------------


def _long_tmpdir(label: str) -> Path:
    """Return a TMPDIR over 90 characters, created under ``/tmp``.

    This is the shape that broke the harness: a caller whose ``TMPDIR`` is long
    enough that ``tempfile.gettempdir()`` -- and therefore pytest's ``tmp_path``
    -- hands out roots no Unix socket fits under.
    """
    path = Path("/tmp") / (f"exo-e2e-longtmpdir-{label}-{os.getpid()}-" + "x" * 70)
    path.mkdir(parents=True, exist_ok=True)
    assert len(str(path)) > 90, str(path)
    return path


def test_a_long_tmpdir_cannot_produce_an_unbindable_socket() -> None:
    """A caller's TMPDIR never decides whether a socket fits.

    ``sun_path`` is 107 bytes, so a root handed out by ``tempfile`` under a long
    ``TMPDIR`` -- the lead's shell had ``TMPDIR=/home/goya/agent-workspace/
    exomonad`` -- yields a socket path the kernel refuses to bind, and the
    failure surfaces as ``OSError: AF_UNIX path too long`` from whichever end
    gets there first. The harness's own roots are pinned to ``/tmp`` instead, so
    this asserts both halves: the inherited root is refused at construction,
    and the harness's roots fit regardless of what ``TMPDIR`` says.
    """
    long_base = _long_tmpdir("refuse")
    monkey_root: Path | None = None
    previous = os.environ.get("TMPDIR")
    try:
        os.environ["TMPDIR"] = str(long_base)
        # What tempfile -- and therefore pytest's tmp_path -- hands a caller.
        # `tempfile` caches its answer, so the cache is pointed at the long
        # directory rather than trusting the variable to have been read.
        previous_tempdir = tempfile.tempdir
        tempfile.tempdir = str(long_base)
        try:
            monkey_root = Path(tempfile.mkdtemp())
        finally:
            tempfile.tempdir = previous_tempdir
        with pytest.raises(tmuxio.TmuxError, match="kernel allows"):
            tmuxio.socket_path(monkey_root)

        # The harness's own roots ignore TMPDIR and still fit.
        run_root = cl.make_root("/tmp", PREFIX)
        short_root = tmuxio.short_root()
        for root in (run_root, short_root):
            socket = tmuxio.socket_path(root)
            assert len(str(socket).encode("utf-8")) <= tmuxio.MAX_SOCKET_PATH_BYTES
            tmuxio.ensure(socket)
            assert socket.parent.is_dir()
            shutil.rmtree(root, ignore_errors=True)
    finally:
        if previous is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous
        if monkey_root is not None:
            shutil.rmtree(monkey_root, ignore_errors=True)
        shutil.rmtree(long_base, ignore_errors=True)


@pytest.mark.skipif(not _tmux_available(), reason="tmux is required")
def test_a_server_starts_under_a_long_tmpdir() -> None:
    """A real server binds under a TMPDIR long enough to break an inherited root.

    The path-length guard is only worth having if the socket it protects is
    still usable, so a session is really started on a root created with TMPDIR
    set to a 90-plus character directory, and really stopped again.
    """
    long_base = _long_tmpdir("serve")
    previous = os.environ.get("TMPDIR")
    root: Path | None = None
    scope: Any = None
    try:
        os.environ["TMPDIR"] = str(long_base)
        root = cl.make_root("/tmp", PREFIX)
        scope = cl.RunScope(
            run_id=f"longtmp{os.getpid()}", root=root, prefix=PREFIX
        )
        session = scope.track_session(scope.session_prefix)
        tmuxio.tmux(
            scope.tmux_socket,
            "new-session",
            "-d",
            "-s",
            session,
            "-n",
            "probe",
            "sleep",
            "600",
            check=True,
        )
        assert tmuxio.server_alive(scope.tmux_socket)
        assert scope.teardown() == []
        assert scope.leaks() == []
    finally:
        if previous is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous
        if scope is not None:
            scope.keep = False
            scope.teardown()
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(long_base, ignore_errors=True)
