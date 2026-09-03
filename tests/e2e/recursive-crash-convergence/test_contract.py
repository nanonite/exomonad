"""Unit checks for the #1057 acceptance contract, without starting infrastructure."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import beast
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
    marker = tmp_path / ".exo" / "1057-nested-heads-case.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"nested-a": expected_head}), encoding="utf-8")
    pulls = [
        {
            "title": "Aggregate nested-a into main.sub-a",
            "head": {"ref": "main.sub-a.nested-a", "sha": "historical-head"},
            "base": {"ref": "main.sub-a"},
        },
        {
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
