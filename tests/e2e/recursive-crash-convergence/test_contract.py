"""Unit checks for the #1057 acceptance contract, without starting infrastructure."""

from __future__ import annotations

import json
import http.server
import socketserver
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import beast
import leaf_publication_agent
import runner
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
        "main.sub-b.sub-b-output",
        "main.sub-c.sub-c-output",
    )
    monkeypatch.setenv(
        "EXOMONAD_1057_LEAF_BRANCHES",
        "main.sub-a.nested-a.nested-output,main.sub-b.sub-b-output",
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    socket_path = tmp_path / "root" / ".exo" / "server.sock"
    socket_path.parent.mkdir(parents=True)
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
    assert len(requests) == 1
    path, body = requests[0]
    assert path == "/agents/tl/nested-output/tools/call"
    assert json.loads(body)["arguments"]["base_branch"] == "main.sub-a.nested-a"


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


def test_beast_root_terminal_contract_allows_adoption_before_root_done() -> None:
    assert not beast._is_terminal("tl_pr_filed")
    assert not beast._is_terminal("tl_finalizing")
    assert beast._is_terminal("tl_done")


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


def test_chainlink_case_database_is_a_non_mutating_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "case" / "issues.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('source')")
    runner._copy_chainlink_database(source, destination)
    with sqlite3.connect(destination) as connection:
        connection.execute("INSERT INTO marker VALUES ('case')")
    with sqlite3.connect(source) as connection:
        values = connection.execute("SELECT value FROM marker").fetchall()
    assert values == [("source",)]


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
