"""Unit checks for the #1057 acceptance contract, without starting infrastructure."""

from __future__ import annotations

import json
import http.server
import shutil
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import leaf_publication_agent
import runner  # inserts PROJECT_ROOT and ordered-recursive onto sys.path
import e2e_harness.tmuxio as tmuxio
import fixture
import real_server_transport as real
from boundaries import (
    CRASH_BOUNDARIES,
    LOGICAL_BOUNDARY_NAMES,
    boundary_for,
    effect_identity,
    redacted_arguments,
    validate_matrix,
)
from crash_transport import CrashBoundaryTransport
from evidence import (
    AcceptanceError,
    assert_checkpoint_progression,
    assert_crash_record,
    assert_effect_cardinality,
    assert_effect_events,
    assert_journal_terminal,
    assert_recursive_effect_cardinality,
    assert_remote_ancestry,
    assert_required_effects,
    assert_resume_not_redispatched,
)


def test_project_root_resolves_to_the_repository_root() -> None:
    """The acceptance must discover target/debug/exomonad and .exo/wasm/."""
    expected = Path(__file__).resolve().parents[3]
    assert runner.PROJECT_ROOT == expected
    # parents[2] is tests/, which made start_server reject every run.
    assert runner.PROJECT_ROOT != Path(__file__).resolve().parents[2]
    assert (runner.PROJECT_ROOT / "Cargo.toml").is_file()
    assert (runner.PROJECT_ROOT / "tl_loop").is_dir()
    # Server discovery is rooted at the repository root, not tests/.
    assert (runner.PROJECT_ROOT / "target" / "debug" / "exomonad").parent == (
        expected / "target" / "debug"
    )
    assert (runner.PROJECT_ROOT / ".exo" / "wasm" / "wasm-guest-devswarm.wasm").parent == (
        expected / ".exo" / "wasm"
    )


def test_fixture_stage_routes_survive_manifest_reconstruction() -> None:
    """A resumed run rebuilds the plan without sources; the guard must still pass."""
    from tl_loop.loop.driver import _plan_consumes_events, _validate_stage_event_routes

    structure = fixture.plan()
    assert all(task.source is None for task in structure.sub_tls)
    first_stage = [task for task in structure.sub_tls if task.order == 1]
    assert len(first_stage) == 2
    assert sum(1 for task in first_stage if _plan_consumes_events(task.plan)) == 1
    # Structure-only is exactly what _work_plan_from_manifest returns on resume;
    # the guard is evaluated per stage with the current stage's pending children.
    _validate_stage_event_routes(first_stage)


def test_fixture_consuming_children_receive_distinct_sources(tmp_path: Path) -> None:
    """Event-consuming children get distinct child-owned ledger sources."""
    from tl_loop.loop.driver import _validate_stage_event_routes

    sourced = fixture.plan(
        segments=tmp_path / "segments",
        state_root=tmp_path / "state" / "root-run",
        ledger_run_id="swarm-1",
    )
    consuming = [task for task in sourced.sub_tls if task.source is not None]
    assert {task.name for task in consuming} == {"sub-a", "sub-c"}
    assert len({id(task.source) for task in consuming}) == len(consuming)
    first_stage = [task for task in sourced.sub_tls if task.order == 1]
    _validate_stage_event_routes(first_stage)


def test_fixture_plan_without_coordinates_has_no_sources() -> None:
    """Seeding uses a structure-only plan; sources are attached only at run time."""
    assert all(task.source is None for task in fixture.plan().sub_tls)


def test_ordered_parent_identity_matches_configured_parent_branch() -> None:
    """The authenticated parent must resolve to the configured branch (main).

    The ordered sub-TL provisioning check compares the caller's resolved birth
    branch with config.branch; the server reads identity.json before the
    parent worktree's own main.parent branch.
    """
    record = real.ordered_parent_identity(Path("/tmp/parent-worktree"))
    assert record["agent_name"] == "parent"
    assert record["birth_branch"] == "main"
    assert record["parent_branch"] == "main"
    assert record["agent_type"] == "codex"
    assert record["topology"] == "worktree_per_agent"
    assert record["ledger_owned"] is True
    assert record["working_dir"] == "/tmp/parent-worktree"


def test_worktree_and_identity_layout_matches_server_owned_paths(tmp_path: Path) -> None:
    """Seeded worktrees/identities must match the server-owned layout."""
    repo = tmp_path / "repo"
    # Ordered sub-TL worktrees nest below their parent under .exo/worktrees.
    assert real.agent_worktree(repo, "main.sub-a") == repo / ".exo/worktrees/sub-a"
    assert real.agent_worktree(repo, "main.sub-a.nested-a") == (
        repo / ".exo/worktrees/sub-a/nested-a"
    )
    # Identities are flat, keyed by agent name.
    assert real.agent_identity_dir(repo, "nested-a") == repo / ".exo/agents/nested-a"
    # Spawned-leaf worktrees stay flat by slug, never inside a parent worktree,
    # or the TL spawn preflight sees an untracked directory.
    parent = real.agent_worktree(repo, "main.sub-a.nested-a")
    leaf = real.agent_leaf_worktree(repo, "main.sub-a.nested-a.nested-output")
    assert leaf == repo / ".exo/worktrees/nested-output"
    assert parent not in leaf.parents


def test_leaf_actor_honors_publication_crash_handoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The leaf actor must inject the publication boundary it is handed."""
    exo = tmp_path / ".exo"
    exo.mkdir(parents=True)
    monkeypatch.setenv("EXOMONAD_SOCKET", str(exo / "server.sock"))

    (exo / "e2e-crash-handoff.json").write_text(
        json.dumps(
            {
                "phase": "crash",
                "boundary": "publication",
                "point": "before",
                "marker": str(tmp_path / "marker.jsonl"),
                "owner_pid": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    handoff = leaf_publication_agent._read_handoff()
    assert (
        leaf_publication_agent._publication_crash_point(handoff, {"title": "Leaf x"})
        == "before"
    )
    # Aggregate publication is the controller's own boundary, not the leaf's.
    assert (
        leaf_publication_agent._publication_crash_point(
            handoff, {"title": "Aggregate x into main"}
        )
        is None
    )

    (exo / "e2e-crash-handoff.json").write_text(
        json.dumps({"phase": "resume", "resume_trace": str(tmp_path / "resume.jsonl")})
        + "\n",
        encoding="utf-8",
    )
    assert (
        leaf_publication_agent._publication_crash_point(
            leaf_publication_agent._read_handoff(), {"title": "Leaf x"}
        )
        is None
    )


def test_two_concurrent_consuming_children_without_sources_are_rejected() -> None:
    """Why the fixture keeps one consuming child per same-order stage.

    A resumed run rebuilds the plan from the manifest, which cannot carry
    ``source`` objects, so two concurrent event-consuming children are always
    rejected. The fixture therefore pairs one consuming child with a
    non-consuming sibling (the working ordered-recursive probe pattern).
    """
    from tl_loop.loop.driver import TLLoopError, _validate_stage_event_routes

    consuming = (
        real.SubTLTask(
            "left", real.WorkPlan(leaves=(real.LeafTask("left-leaf", "x"),)), order=1
        ),
        real.SubTLTask(
            "right", real.WorkPlan(leaves=(real.LeafTask("right-leaf", "x"),)), order=1
        ),
    )
    with pytest.raises(TLLoopError):
        _validate_stage_event_routes(consuming)


def test_every_logical_boundary_has_before_and_after_process_death() -> None:
    validate_matrix()
    assert len(CRASH_BOUNDARIES) == 2 * len(LOGICAL_BOUNDARY_NAMES)
    for name in LOGICAL_BOUNDARY_NAMES:
        assert {boundary_for(name, point).point for point in ("before", "after")} == {
            "before",
            "after",
        }


def test_spawn_boundary_matches_real_child_process_effects_only(tmp_path: Path) -> None:
    transport = CrashBoundaryTransport(
        tmp_path,
        tmp_path / "crash.jsonl",
        boundary_for("spawn", "before"),
    )
    assert transport._matches("spawn_leaf", {})
    assert transport._matches("spawn_worker", {})
    assert not transport._matches(
        "emit_controller_event", {"event_type": "tl.dispatch_confirmed"}
    )


def test_boundary_lookup_and_effect_identity_are_canonical() -> None:
    with pytest.raises(KeyError):
        boundary_for("not-a-real-effect", "before")
    first = effect_identity({"intent_id": "x", "body": "secret", "child_id": "a"})
    second = effect_identity({"child_id": "a", "intent_id": "x"})
    assert first == second


def test_leaf_publication_actor_is_limited_to_recursive_leaf_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert runner._leaf_branches(runner.plan()) == (
        "main.sub-a.nested-a.nested-output",
        "main.sub-c.sub-c-output",
    )
    monkeypatch.setenv(
        "EXOMONAD_1057_LEAF_BRANCHES",
        "main.sub-a.nested-a.nested-output,main.sub-c.sub-c-output",
    )
    assert leaf_publication_agent._target_leaf_branch(
        "main.sub-a.nested-a.nested-output"
    )
    assert not leaf_publication_agent._target_leaf_branch("main.sub-a.nested-a")
    assert not leaf_publication_agent._target_leaf_branch("review-pr-43")
    assert "body" not in redacted_arguments({"body": "secret", "child_id": "a"})
    nested = redacted_arguments(
        {
            "event_type": "pr.review",
            "payload": {"review_id": 7, "body": "secret", "findings": ["secret"]},
        }
    )
    assert nested["payload"]["review_id"] == 7
    assert nested["payload"]["body"] == "<redacted>"
    assert nested["payload"]["findings"] == "<redacted>"


def test_leaf_publication_uses_the_explicit_root_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actor talks to the root socket, on a root short enough to bind.

    The root is not ``tmp_path``: a Unix socket path is at most 107 bytes, and
    ``tmp_path`` descends from ``TMPDIR``, which a caller may have made long
    enough that nothing socket-shaped fits. A short root under ``/tmp`` is used
    instead and removed whatever the test does.
    """
    requests: list[tuple[str, bytes]] = []

    class UnixHTTPHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers["Content-Length"])
            requests.append((self.path, self.rfile.read(length)))
            response = b'{"success": true, "result": {}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_: object) -> None:
            return

    class UnixHTTPServer(socketserver.UnixStreamServer):
        allow_reuse_address = True

    root = tmuxio.short_root()
    socket_path = root / "root" / ".exo" / "server.sock"
    socket_path.parent.mkdir(parents=True)
    assert len(str(socket_path).encode("utf-8")) <= tmuxio.MAX_SOCKET_PATH_BYTES
    server = UnixHTTPServer(str(socket_path), UnixHTTPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "EXOMONAD_1057_LEAF_BRANCHES", "main.sub-a.nested-a.nested-output"
    )
    monkeypatch.setenv("EXOMONAD_SOCKET", str(socket_path))
    monkeypatch.setattr(
        leaf_publication_agent,
        "_current_branch",
        lambda: "main.sub-a.nested-a.nested-output",
    )
    monkeypatch.setattr(leaf_publication_agent, "_current_head", lambda: "leaf-head")
    try:
        assert leaf_publication_agent.publish_leaf()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        shutil.rmtree(root, ignore_errors=True)
    assert len(requests) == 1
    path, body = requests[0]
    assert path == "/agents/tl/nested-output/tools/call"
    assert json.loads(body)["arguments"]["base_branch"] == "main.sub-a.nested-a"


def test_leaf_records_file_pr_attempt_before_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publication:before crash must not lose the surviving leaf's call."""
    exo = tmp_path / ".exo"
    exo.mkdir(parents=True)
    monkeypatch.setenv("EXOMONAD_SOCKET", str(exo / "server.sock"))
    monkeypatch.setenv(
        "EXOMONAD_1057_LEAF_BRANCHES", "main.sub-a.nested-a.nested-output"
    )
    monkeypatch.setattr(
        leaf_publication_agent,
        "_current_branch",
        lambda: "main.sub-a.nested-a.nested-output",
    )
    monkeypatch.setattr(leaf_publication_agent, "_current_head", lambda: "leaf-head")
    marker = tmp_path / "marker.jsonl"
    resume = tmp_path / "resume.jsonl"
    (exo / "e2e-crash-handoff.json").write_text(
        json.dumps(
            {
                "phase": "crash",
                "boundary": "publication",
                "point": "before",
                "marker": str(marker),
                "resume_trace": str(resume),
                "owner_pid": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    calls: list[str] = []

    class FakeResult:
        success = True
        error = None

    class FakeClient:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def file_pr(self, **_: object) -> FakeResult:
            # The attempt and the crash boundary must already be durable before
            # the call is issued.
            assert resume.is_file(), "file_pr attempt was not recorded before the call"
            assert marker.is_file(), "crash boundary was not recorded before the call"
            calls.append("call")
            return FakeResult()

    monkeypatch.setattr(leaf_publication_agent, "EffectClient", FakeClient)

    assert leaf_publication_agent.publish_leaf()
    assert calls == ["call"]
    records = [
        json.loads(line)
        for line in resume.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["tool_name"] == "file_pr"
    assert records[0]["crash_point"] == "before"
    assert len(marker.read_text(encoding="utf-8").splitlines()) == 1


def test_leaf_publication_requires_the_root_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMONAD_SOCKET", raising=False)
    with pytest.raises(
        leaf_publication_agent.LeafPublicationError, match="EXOMONAD_SOCKET"
    ):
        leaf_publication_agent._server_socket()


def test_reviewer_actor_approves_the_authoritative_current_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORGEJO_URL", "http://forgejo")
    monkeypatch.setenv("FORGEJO_OWNER", "owner")
    monkeypatch.setenv("FORGEJO_REPO", "repo")
    monkeypatch.setenv("FORGEJO_REVIEWER_TOKEN", "reviewer-token")
    requests: list[tuple[str, str, object, str]] = []

    def fake_request(
        method: str,
        url: str,
        *,
        token: str,
        payload: object = None,
    ) -> object:
        requests.append((method, url, payload, token))
        if url.endswith("/api/v1/user"):
            return {"login": "reviewer"}
        if url.endswith("/pulls/43"):
            return {"head": {"sha": "exact-head"}}
        if url.endswith("/pulls/43/reviews"):
            return []
        return {"id": 12}

    monkeypatch.setattr(leaf_publication_agent, "_request", fake_request)
    assert leaf_publication_agent.review_assigned_pr(43)
    assert requests[-1] == (
        "POST",
        "http://forgejo/api/v1/repos/owner/repo/pulls/43/reviews",
        {"event": "APPROVED", "commit_id": "exact-head"},
        "reviewer-token",
    )


def test_non_target_actor_remains_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMONAD_1057_LEAF_BRANCHES", raising=False)
    monkeypatch.setattr(
        leaf_publication_agent, "_current_branch", lambda: "review-pr-43"
    )
    monkeypatch.setattr(sys, "argv", ["leaf_publication_agent.py"])
    slept: list[float] = []
    monkeypatch.setattr(leaf_publication_agent.time, "sleep", slept.append)
    assert leaf_publication_agent.main() == 0
    assert slept == [300]


def test_crash_record_requires_one_identity(tmp_path: Path) -> None:
    marker = tmp_path / "crash.jsonl"
    marker.write_text(
        json.dumps({"boundary": "push", "point": "after", "identity": "sha"}) + "\n",
        encoding="utf-8",
    )
    assert assert_crash_record(marker, "push", "after") == "sha"
    with pytest.raises(AcceptanceError, match="expected one"):
        assert_crash_record(marker, "push", "before")


def test_journal_and_checkpoint_assertions_reject_duplicate_or_regressed_state(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "action-journal.json"
    journal.write_text(
        json.dumps([{"key": "a", "operation": "merge_pr", "status": "confirmed"}]),
        encoding="utf-8",
    )
    assert assert_journal_terminal(journal)["merge_pr"] == 1
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(
        json.dumps({"state_version": 1, "events": {"last_consumed_offset": 4}}),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps({"state_version": 2, "events": {"last_consumed_offset": 5}}),
        encoding="utf-8",
    )
    assert_checkpoint_progression([first, second])
    second.write_text(
        json.dumps({"state_version": 0, "events": {"last_consumed_offset": 5}}),
        encoding="utf-8",
    )
    with pytest.raises(AcceptanceError, match="regressed"):
        assert_checkpoint_progression([first, second])


def test_effect_cardinality_rejects_a_second_attempt_in_one_generation(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "action-journal.json"
    journal.write_text(
        json.dumps(
            [
                {
                    "key": "first",
                    "operation": "post_merge_push",
                    "target": "child-a",
                    "arguments": {"child_id": "child-a", "generation": 2},
                    "status": "confirmed",
                },
                {
                    "key": "second",
                    "operation": "post_merge_push",
                    "target": "child-a",
                    "arguments": {"child_id": "child-a", "generation": 2},
                    "status": "confirmed",
                },
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(AcceptanceError, match="more than once"):
        assert_effect_cardinality(journal)


def test_recursive_effect_cardinality_covers_nested_scope_journals(
    tmp_path: Path,
) -> None:
    root_journal = tmp_path / "root" / "action-journal.json"
    child_journal = tmp_path / "root" / "child" / "action-journal.json"
    for path, target in ((root_journal, "root"), (child_journal, "child")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    {
                        "key": target,
                        "operation": "root_branch_finalize",
                        "target": target,
                        "arguments": {"generation": 1},
                        "status": "confirmed",
                    }
                ]
            ),
            encoding="utf-8",
        )
    assert assert_recursive_effect_cardinality(tmp_path / "root") == {
        "root_branch_finalize": 2
    }


def test_required_effects_accept_any_real_spawn_tool_and_reject_missing_families() -> (
    None
):
    counts = {
        "spawn_leaf": 1,
        "file_pr": 1,
        "resume_pr": 1,
        "merge_pr": 1,
        "chainlink_issue_close": 1,
        "post_merge_parent_sync": 1,
        "post_merge_remote_reconcile": 1,
        "post_merge_changelog": 1,
        "post_merge_push": 1,
        "root_branch_finalize": 1,
    }
    assert_required_effects(counts)
    del counts["spawn_leaf"]
    with pytest.raises(AcceptanceError, match="effect groups"):
        assert_required_effects(counts)


def test_remote_ancestry_requires_durable_head_and_proof() -> None:
    with pytest.raises(AcceptanceError, match="ancestry evidence"):
        assert_remote_ancestry({})
    assert_remote_ancestry(
        {
            "post_merge": {
                "evidence": {
                    "remote_head_sha": "abcdef1",
                    "ancestry_proof": "ancestor:abcdef1->abcdef1",
                }
            }
        }
    )


def test_remote_ancestry_rejects_unrelated_recorded_remote_head() -> None:
    with pytest.raises(AcceptanceError, match="not the descendant"):
        assert_remote_ancestry(
            {
                "remote_head_sha": "abcdef1",
                "ancestry_proof": "ancestor:abcdef1->abcdef2",
            }
        )


def test_remote_ancestry_checks_the_authoritative_git_ref(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    workspace = tmp_path / "workspace"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace)], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.name", "acceptance"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "config",
            "user.email",
            "acceptance@example.com",
        ],
        check=True,
    )
    (workspace / "evidence.txt").write_text("remote evidence\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "evidence.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-q", "-m", "Evidence"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "remote", "add", "origin", str(remote)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "push", "-q", "origin", "main"],
        check=True,
    )
    sha = subprocess.check_output(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True
    ).strip()
    document = {"remote_head_sha": sha, "ancestry_proof": f"ancestor:{sha}->{sha}"}
    assert_remote_ancestry(
        document,
        workspace=workspace,
        remote=str(remote),
        remote_branch="main",
    )
    (workspace / "unpublished.txt").write_text(
        "not pushed to the authoritative branch\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(workspace), "add", "unpublished.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-q", "-m", "Unpublished"],
        check=True,
    )
    unpublished_sha = subprocess.check_output(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True
    ).strip()
    with pytest.raises(AcceptanceError, match="authoritative remote head"):
        assert_remote_ancestry(
            {
                "remote_head_sha": unpublished_sha,
                "ancestry_proof": f"ancestor:{sha}->{unpublished_sha}",
            },
            workspace=workspace,
            remote=str(remote),
            remote_branch="main",
        )


def test_nested_aggregate_assertion_ignores_historical_pr_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected_head = "current-nested-head"
    marker = tmp_path / ".exo" / "1057-nested-baseline-heads-case.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"nested-a": "seed-head"}), encoding="utf-8")
    state_root = tmp_path / "controller-state"
    checkpoint = state_root / "nested-a" / "run.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(
        json.dumps(
            {
                "integration": {
                    "integration_owner_run_id": "nested-a",
                    "integration_owner_branch": "main.sub-a.nested-a",
                    "aggregate_pr_number": 7,
                    "aggregate_head_sha": expected_head,
                }
            }
        ),
        encoding="utf-8",
    )
    pulls = [
        {
            "number": 6,
            "title": "Aggregate nested-a into main.sub-a",
            "head": {"ref": "main.sub-a.nested-a", "sha": "historical-head"},
            "base": {"ref": "main.sub-a"},
        },
        {
            "number": 7,
            "title": "Aggregate nested-a into main.sub-a",
            "head": {"ref": "main.sub-a.nested-a", "sha": expected_head},
            "base": {"ref": "main.sub-a"},
        },
    ]
    monkeypatch.setattr(runner.real, "json_request", lambda *args, **kwargs: pulls)
    runner._assert_nested_aggregate_pr(
        {
            "EXOMONAD_FORGEJO_E2E_OWNER": "owner",
            "EXOMONAD_FORGEJO_E2E_REPO": "repo",
            "EXOMONAD_FORGEJO_E2E_TOKEN": "token",
        },
        "http://forgejo",
        tmp_path,
        "case",
        state_root,
    )


def test_the_case_database_is_created_fresh_and_seeds_what_the_case_needs(
    tmp_path: Path,
) -> None:
    """A case starts from `chainlink init`, never from a copy of another run.

    A copied database carries rows another run created, so a case could pass on
    evidence it did not produce. The database is created inside the case's own
    directory, seeded with exactly the issue the case needs, and disappears with
    the case.
    """
    database = runner.chainlink_db.create(tmp_path)
    assert database.is_file()
    assert database.is_relative_to(tmp_path)
    seeded = runner.chainlink_db.seed(
        database,
        (
            {
                "title": "Verify recursive crash convergence fresh seed",
                "priority": "low",
                "labels": ("test",),
            },
        ),
    )
    assert len(seeded) == 1 and seeded[0] > 0
    state = runner.chainlink_db.issue_state(database, seeded[0])
    assert state["title"] == "Verify recursive crash convergence fresh seed"
    assert len(runner.chainlink_db.database_files(database)) >= 1
    runner.chainlink_db.remove(database)
    assert not database.exists()


def test_a_case_never_reads_an_operator_chainlink_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHAINLINK_DB is not an input: the harness builds its own and says so."""
    for name in (
        "EXOMONAD_FORGEJO_E2E_URL",
        "EXOMONAD_FORGEJO_E2E_TOKEN",
        "EXOMONAD_FORGEJO_E2E_REVIEWER_TOKEN",
        "EXOMONAD_FORGEJO_E2E_OWNER",
        "EXOMONAD_FORGEJO_E2E_REPO",
        "EXOMONAD_FORGEJO_E2E_GIT_REMOTE",
    ):
        monkeypatch.setenv(name, "set")
    monkeypatch.delenv("CHAINLINK_DB", raising=False)
    monkeypatch.delenv("EXOMONAD_FORGEJO_E2E_MOCK", raising=False)
    config = runner._environment()
    assert "CHAINLINK_DB" not in config
    assert not hasattr(runner, "_copy_chainlink_database")




def test_effect_event_assertion_requires_one_merge_lifecycle(tmp_path: Path) -> None:
    segments = tmp_path / ".exo" / "ledger" / "segments"
    segments.mkdir(parents=True)
    events = [
        {
            "run_id": "swarm",
            "type": "tl.action_queued",
            "data": {"action": "merge", "action_key": "m"},
        },
        {"run_id": "swarm", "type": "tl.merge_decided", "data": {"decision": "merge"}},
        {"run_id": "swarm", "type": "tl.merge_reconciled", "data": {}},
    ]
    (segments / "segment.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )
    assert assert_effect_events(tmp_path, "swarm") == {
        "merge_intents": 1,
        "merge_decisions": 1,
        "merge_reconciliations": 1,
    }


def test_resume_trace_enforces_before_and_after_effect_cardinality(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "resume.jsonl"
    trace.write_text(json.dumps({"identity": "same"}) + "\n", encoding="utf-8")
    assert (
        assert_resume_not_redispatched(
            trace, "same", boundary="remote_merge", point="before"
        )
        == 1
    )
    with pytest.raises(AcceptanceError, match="cardinality"):
        assert_resume_not_redispatched(
            trace, "same", boundary="remote_merge", point="after"
        )


def _publication_attempt(identity: str = "leaf-file-pr", **overrides: object) -> str:
    record: dict[str, object] = {
        "identity": identity,
        "tool_name": "file_pr",
        "crash_point": "before",
    }
    record.update(overrides)
    return json.dumps(record)


def _write_trace(path: Path, *rows: str) -> Path:
    path.write_text("".join(f"{row}\n" for row in rows), encoding="utf-8")
    return path


def test_resume_trace_accepts_exactly_one_publication_actor_attempt(
    tmp_path: Path,
) -> None:
    """Both crash points keep the exact-one actor-attempt rule from af6fa425."""
    before = _write_trace(
        tmp_path / "before.jsonl", _publication_attempt(crash_point="before")
    )
    after = _write_trace(
        tmp_path / "after.jsonl", _publication_attempt(crash_point="after")
    )

    assert (
        assert_resume_not_redispatched(
            before, "leaf-file-pr", boundary="publication", point="before"
        )
        == 1
    )
    assert (
        assert_resume_not_redispatched(
            after, "leaf-file-pr", boundary="publication", point="after"
        )
        == 1
    )


def test_publication_evidence_rejects_a_missing_actor_attempt(tmp_path: Path) -> None:
    """The escaped-call bug: the surviving leaf's call was never recorded."""
    empty = _write_trace(tmp_path / "empty.jsonl")

    with pytest.raises(AcceptanceError, match="publication effect cardinality"):
        assert_resume_not_redispatched(
            empty, "leaf-file-pr", boundary="publication", point="before"
        )


def test_publication_evidence_rejects_a_duplicate_actor_attempt(tmp_path: Path) -> None:
    """Two attempts for one crashed publication are a duplicate publication."""
    duplicated = _write_trace(
        tmp_path / "duplicated.jsonl",
        _publication_attempt(crash_point="after"),
        _publication_attempt(crash_point="after"),
    )

    with pytest.raises(AcceptanceError, match="publication effect cardinality"):
        assert_resume_not_redispatched(
            duplicated, "leaf-file-pr", boundary="publication", point="after"
        )


def test_publication_evidence_rejects_another_tools_attempt(tmp_path: Path) -> None:
    """Only a file_pr attempt can prove the leaf published exactly once."""
    aggregate = _write_trace(
        tmp_path / "aggregate.jsonl", _publication_attempt(tool_name="merge_pr")
    )

    with pytest.raises(AcceptanceError, match="not a file_pr attempt"):
        assert_resume_not_redispatched(
            aggregate, "leaf-file-pr", boundary="publication", point="before"
        )


def test_publication_evidence_rejects_another_crash_point(tmp_path: Path) -> None:
    """A record from the other crash point belongs to a different boundary."""
    other_point = _write_trace(
        tmp_path / "other-point.jsonl", _publication_attempt(crash_point="after")
    )
    untagged = _write_trace(
        tmp_path / "untagged.jsonl", _publication_attempt(crash_point=None)
    )

    for trace in (other_point, untagged):
        with pytest.raises(AcceptanceError, match="not from the publication:before"):
            assert_resume_not_redispatched(
                trace, "leaf-file-pr", boundary="publication", point="before"
            )


def test_publication_evidence_rejects_an_unregistered_crash_point(
    tmp_path: Path,
) -> None:
    """A crash point outside the matrix never authorizes a binding claim."""
    trace = _write_trace(tmp_path / "matrix.jsonl", _publication_attempt())

    with pytest.raises(AcceptanceError, match="unregistered publication crash point"):
        assert_resume_not_redispatched(
            trace, "leaf-file-pr", boundary="publication", point="during"
        )


def test_resume_trace_rejects_malformed_and_untyped_rows(tmp_path: Path) -> None:
    """A dropped or corrupt row would hide the redispatch evidence."""
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text("{not json\n", encoding="utf-8")
    untyped = _write_trace(tmp_path / "untyped.jsonl", json.dumps(["leaf-file-pr"]))

    with pytest.raises(AcceptanceError, match="malformed"):
        assert_resume_not_redispatched(
            malformed, "leaf-file-pr", boundary="publication", point="before"
        )
    with pytest.raises(AcceptanceError, match="not an object"):
        assert_resume_not_redispatched(
            untyped, "leaf-file-pr", boundary="publication", point="before"
        )


def test_resume_trace_rejects_a_missing_file(tmp_path: Path) -> None:
    """A missing trace proves nothing about redispatch."""
    with pytest.raises(AcceptanceError, match="resume call trace is missing"):
        assert_resume_not_redispatched(
            tmp_path / "absent.jsonl",
            "leaf-file-pr",
            boundary="publication",
            point="before",
        )


def test_every_leg_names_its_items_and_its_own_valid_plan() -> None:
    """Each leg is one acceptance shape: its own items, its own plan document.

    ``recreate`` is the #1117 scenario, ``control`` the same dispatch with no
    recreate (#1138 step 3), and ``child`` the #1112 shape with the leaf under
    a child sub-TL (#1138 step 4). A leg that silently ran another leg's items,
    or a plan the shipped preflight would refuse, is not the shape it claims.
    """
    import driver
    import scenario
    from run_prefix import CHILD_SUB_TL, LEGS

    from tl_loop.plan_validation import validate_plan_document

    assert tuple(driver.ITEMS_BY_LEG) == LEGS
    for leg in LEGS:
        items = driver.ITEMS_BY_LEG[leg]
        assert items, f"{leg} runs no items"
        assert set(items) <= set(driver.ITEMS), f"{leg} names an unknown item"
    recreate_only = {
        "confirmed_recreate",
        "new_dispatch",
        "publish_pr_b",
        "no_adoption_of_pr_a",
    }
    assert not recreate_only & set(driver.ITEMS_BY_LEG["control"])
    assert recreate_only <= set(driver.ITEMS_BY_LEG["child"])

    for leg in LEGS:
        document = scenario._plan_document(leg)
        assert validate_plan_document(document)["run_id"] == "root"
    child_leaves = scenario._plan_document("child")["plan"]["sub_tls"][0]["plan"]["leaves"]
    assert [leaf["name"] for leaf in child_leaves] == [scenario.LEAF_SLICE]
    # The shipped spawn path derives a leaf branch from its owning scope, so
    # the child leg's leaf hangs off the child sub-TL's branch, not off main.
    assert scenario._leaf_branch("child") == f"main.{CHILD_SUB_TL}.{scenario.LEAF_SLICE}-codex"
    assert scenario._leaf_branch("recreate") == f"main.{scenario.LEAF_SLICE}-codex"
    assert scenario._leaf_branch("control") == scenario._leaf_branch("recreate")


def test_the_spawned_reviewer_leaves_this_runs_approval_to_the_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance posts its own approval, so its stand-in must not post one.

    The shipped controller spawns its reviewer as soon as a slice reaches
    review, and that reviewer resolves through this harness's shim; if it
    submitted too, the run would hold two approvals for one head and the
    acceptance's "exactly one" would be its own doing.
    """
    monkeypatch.setenv("EXOMONAD_REVIEW_OWNED_BY_HARNESS", "1")
    monkeypatch.setattr(sys, "argv", ["leaf_publication_agent.py", "Review PR #7: task"])

    def forbid_submission(pr_number: int) -> bool:
        raise AssertionError(f"the stand-in submitted an approval for PR #{pr_number}")

    monkeypatch.setattr(leaf_publication_agent, "review_assigned_pr", forbid_submission)
    assert leaf_publication_agent.main() == 0


def test_the_reviewer_stand_in_still_approves_a_run_that_owns_no_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that does not opt out keeps the reviewer actor's own approval."""
    monkeypatch.delenv("EXOMONAD_REVIEW_OWNED_BY_HARNESS", raising=False)
    monkeypatch.setattr(sys, "argv", ["leaf_publication_agent.py", "Review PR #7: task"])
    submitted: list[int] = []
    monkeypatch.setattr(
        leaf_publication_agent, "review_assigned_pr", lambda n: submitted.append(n)
    )
    assert leaf_publication_agent.main() == 0
    assert submitted == [7]


def test_the_database_is_created_where_the_shipped_controller_is_anchored(
    tmp_path: Path,
) -> None:
    """`exomonad init` anchors CHAINLINK_DB to `<project>/.chainlink`.

    That is where `build_spawn_env` points every spawned agent too, so a run
    that keeps its database anywhere else reads a file its own escalations
    never touch -- and if the anchored directory does not exist, the
    controller dies inside `park()` before it can park anything.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    database = runner.chainlink_db.create(tmp_path, project_dir=repo)

    assert database == repo / ".chainlink" / "issues.db"
    assert database == runner.chainlink_db.database_path_for(repo)
    assert runner.chainlink_db.project_dir(database) == repo
    assert database.is_file()
