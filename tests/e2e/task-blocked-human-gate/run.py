#!/usr/bin/env python3
"""Real-server transport acceptance for blocked handoff and same-owner resume.

The harness deliberately injects the typed blocked and parked events through the
real MCP transport. It validates persistence, restart, and owner resumption;
the TL controller and CI watcher decision are covered by their focused tests.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ORDERED_DIR = PROJECT_ROOT / "tests/e2e/ordered-recursive"
sys.path.insert(0, str(ORDERED_DIR))

import real_server_transport as real

from tl_loop.client.effects import EffectClient, ToolResult
from tl_loop.events.reader import LedgerReader

SLICE_ID = "base-ci-blocked-leaf"
OWNER_HINT = "base-ci-blocked-leaf"


def wait_until(predicate: Any, description: str, timeout: float = 20.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise real.HarnessError(f"timed out waiting for {description}")


def invocation(agent_dir: Path) -> dict[str, Any]:
    return json.loads((agent_dir / "invocation.json").read_text(encoding="utf-8"))


def identity(agent_dir: Path) -> dict[str, Any]:
    return json.loads((agent_dir / "identity.json").read_text(encoding="utf-8"))


def owner_for(repo: Path) -> Path:
    def find() -> Path | None:
        for candidate in (repo / ".exo/agents").iterdir():
            path = candidate / "identity.json"
            if not path.is_file():
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(record, Mapping) and record.get("slug") == OWNER_HINT:
                return candidate
        return None

    return wait_until(find, "real blocked leaf owner")


def worktree_for(repo: Path, agent_dir: Path) -> Path:
    record = identity(agent_dir)
    configured = record.get("working_dir")
    if isinstance(configured, str) and configured:
        candidate = Path(configured)
        if not candidate.is_absolute():
            candidate = repo / candidate
        if candidate.is_dir():
            return candidate
    slug = str(record.get("slug") or OWNER_HINT)
    candidate = repo / ".exo/worktrees" / slug
    if not candidate.is_dir():
        raise real.HarnessError(f"owner worktree is missing: {candidate}")
    return candidate


def worktree_fingerprint(worktree: Path) -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=worktree,
        capture_output=True,
        check=True,
    ).stdout
    return "sha256:" + hashlib.sha256(status).hexdigest()


def kill_owner_target(agent_dir: Path) -> None:
    routing = json.loads((agent_dir / "routing.json").read_text(encoding="utf-8"))
    target = routing.get("pane_id") or routing.get("window_id")
    if not isinstance(target, str) or not target:
        raise real.HarnessError(f"owner has no exact tmux target: {routing!r}")
    command = "kill-pane" if target.startswith("%") else "kill-window"
    subprocess.run(["tmux", command, "-t", target], check=True, capture_output=True)


def issue_number(result: ToolResult) -> int:
    for candidate in real.json_objects(result.raw):
        for key in ("issue_id", "number", "id", "cicoIssueId"):
            value = candidate.get(key)
            if type(value) is int and value > 0:
                return value
    raise real.HarnessError(
        f"issue creation returned no numeric identity: {result.raw!r}"
    )


def typed_events(repo: Path, event_type: str) -> list[dict[str, Any]]:
    """Read mapped server events through the canonical ledger projection."""
    run_id = real.server_run_id(repo)
    reader = LedgerReader(repo / ".exo/ledger/segments", ledger_run_id=run_id)
    return [
        {"data": dict(event.data), "event_type": event.event_type}
        for event in reader.read_from(0).events
        if event.event_type == event_type
    ]


def blocked_payload(repo: Path, slice_id: str) -> dict[str, Any]:
    rows = typed_events(repo, "agent.task_blocked")
    matching = [
        row.get("data")
        for row in rows
        if isinstance(row.get("data"), Mapping)
        and row["data"].get("slice_id") == slice_id
    ]
    if len(matching) != 1 or not isinstance(matching[0], dict):
        raise real.HarnessError(f"expected one typed blocked event, got {rows!r}")
    return matching[0]


def resume_blocked_leaf(
    client: real.TransportClient, args: dict[str, Any]
) -> ToolResult:
    return ToolResult.from_raw(
        client.call_tool("tl", "root", "resume_blocked_leaf", args)
    )


def stop_server_and_session(process: subprocess.Popen[str], repo: Path) -> None:
    real.stop_subprocess(process, "blocked-handoff server")
    config = (repo / ".exo/config.toml").read_text(encoding="utf-8")
    session = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in config.splitlines()
        if line.startswith("tmux_session =")
    )
    subprocess.run(["tmux", "kill-session", "-t", session], check=False)


def cleanup_server_scaffolding(repo: Path) -> None:
    for name in ("sub-a", "sub-b", "recursive-root", "nested", "parent"):
        worktree = (
            repo / ".exo/agents" / name
            if name != "parent"
            else repo / ".exo/worktrees/parent"
        )
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "branch", "-D", f"main.{name}"],
            check=False,
            capture_output=True,
        )


def run_case(index: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(
        prefix=f"exomonad-task-blocked-{index}-"
    ) as raw_root:
        root = Path(raw_root)
        repo, remote, _ = real.create_fixture(root)
        (repo / ".chainlink").mkdir(parents=True, exist_ok=True)
        gitignore = repo / ".gitignore"
        gitignore.write_text(
            gitignore.read_text(encoding="utf-8") + ".chainlink/\n",
            encoding="utf-8",
        )
        real.git(repo, "add", ".gitignore")
        real.git(repo, "commit", "-q", "-m", "Ignore disposable Chainlink runtime")
        real.git(repo, "update-ref", "refs/heads/main", "HEAD")
        mock, forgejo = real.start_mock(root, PROJECT_ROOT, remote)
        server: subprocess.Popen[str] | None = None
        owner_dir: Path | None = None
        worktree: Path | None = None
        try:
            server, client = real.start_server(root, repo, forgejo, PROJECT_ROOT)
            effects = EffectClient(client, role="tl", name="root")
            created = effects.chainlink_issue_create(
                title="E2E base CI blocked handoff",
                description="Disposable acceptance issue; close after cleanup.",
                labels=("bug",),
                priority="high",
            )
            if not created.success:
                raise real.HarnessError(
                    f"real Chainlink issue creation failed: {created.raw!r}"
                )
            blocked_issue_id = issue_number(created)
            spawned = effects.spawn_leaf(
                name=OWNER_HINT,
                task=f"Work on Chainlink issue #{blocked_issue_id}; preserve base-CI attribution.",
                agent_type="codex",
            )
            if not spawned.success:
                raise real.HarnessError(f"real leaf spawn failed: {spawned.raw!r}")
            owner_dir = owner_for(repo)
            worktree = worktree_for(repo, owner_dir)
            (owner_dir / "active_issue").write_text(
                str(blocked_issue_id), encoding="utf-8"
            )
            (worktree / "uncommitted-blocked-change.txt").write_text(
                "base CI is independently failing\n", encoding="utf-8"
            )
            initial = invocation(owner_dir)
            branch = str(identity(owner_dir)["birth_branch"])
            fingerprint = worktree_fingerprint(worktree)
            kill_owner_target(owner_dir)
            wait_until(
                lambda: (
                    invocation(owner_dir).get("status")
                    in {"exited", "failed", "killed"}
                ),
                "blocked invocation to become terminal",
            )
            terminal = invocation(owner_dir)
            if terminal.get("status") not in {"exited", "failed", "killed"}:
                raise real.HarnessError(
                    f"blocked invocation is not terminal: {terminal!r}"
                )

            parked = effects.emit_controller_event(
                event_type="tl.slice_parked",
                payload={
                    "slice_id": SLICE_ID,
                    "park_cause": "base_ci_unstable",
                    "attempts": 1,
                    "reason": "base CI failed independently of the leaf",
                },
            )
            blocked = effects.emit_controller_event(
                event_type="agent.task_blocked",
                payload={
                    "outcome": "blocked",
                    "slice_id": SLICE_ID,
                    "cause": "base_ci_unstable",
                    "scope_attribution": "base",
                    "needs_human": True,
                    "retryable": True,
                    "recovery_action": "stabilize base CI, then resume the parked owner",
                    "declared_difficulty": "standard",
                    "matched_difficulty_rule": "standard_slice",
                    "attempt": 1,
                },
            )
            if not parked.success or not blocked.success:
                raise real.HarnessError(
                    f"real blocked events failed: {parked.raw!r} {blocked.raw!r}"
                )
            payload = blocked_payload(repo, SLICE_ID)
            if (
                payload.get("cause") != "base_ci_unstable"
                or payload.get("needs_human") is not True
            ):
                raise real.HarnessError(
                    f"typed blocked payload lost attribution: {payload!r}"
                )
            if (
                payload.get("scope_attribution") != "base"
                or payload.get("declared_difficulty") != "standard"
            ):
                raise real.HarnessError(
                    f"difficulty attribution was not external: {payload!r}"
                )
            if terminal.get("exit_code") == 0:
                raise real.HarnessError("killed invocation unexpectedly exited cleanly")
            if len(typed_events(repo, "agent.task_blocked")) != 1:
                raise real.HarnessError("blocked telemetry was duplicated")

            # Restart the real server against the same durable DB and ledger.
            stop_server_and_session(server, repo)
            server = None
            cleanup_server_scaffolding(repo)
            shutil.rmtree(root / "fake-bin", ignore_errors=True)
            server, restarted = real.start_server(root, repo, forgejo, PROJECT_ROOT)
            restarted_effects = EffectClient(restarted, role="tl", name="root")
            shown = restarted_effects.chainlink_issue_show(issue_id=blocked_issue_id)
            if not shown.success:
                raise real.HarnessError("restart lost the durable human issue")
            if (
                len(
                    list(
                        real.json_objects(
                            restarted_effects.chainlink_issue_list(status="open").raw
                        )
                    )
                )
                == 0
            ):
                raise real.HarnessError(
                    "restart did not retain an open human gate issue"
                )
            open_gate_rows = list(
                real.json_objects(
                    restarted_effects.chainlink_issue_list(status="open").raw
                )
            )
            gate_reused = any(
                any(
                    candidate.get(key) == blocked_issue_id
                    for key in ("issue_id", "number", "id", "cicoIssueId")
                )
                for candidate in open_gate_rows
            )
            if not gate_reused:
                raise real.HarnessError(
                    "restart did not retain the original human gate identity"
                )

            stale = resume_blocked_leaf(
                restarted,
                {
                    "chainlink_issue_id": blocked_issue_id,
                    "expected_invocation_id": "stale-invocation",
                    "expected_branch": branch,
                    "expected_worktree_fingerprint": fingerprint,
                    "task": "stale resume must fail closed",
                    "human_approved": True,
                },
            )
            if stale.success is not False:
                raise real.HarnessError(
                    "stale invocation resume unexpectedly succeeded"
                )
            resumed = resume_blocked_leaf(
                restarted,
                {
                    "chainlink_issue_id": blocked_issue_id,
                    "expected_invocation_id": initial["invocation_id"],
                    "expected_branch": branch,
                    "expected_worktree_fingerprint": fingerprint,
                    "task": "Stabilize base CI, then continue the same leaf assignment.",
                    "human_approved": True,
                },
            )
            if not resumed.success:
                raise real.HarnessError(f"same-owner resume failed: {resumed.raw!r}")
            fresh = invocation(owner_dir)
            if (
                fresh["invocation_id"] == initial["invocation_id"]
                or fresh.get("status") != "running"
            ):
                raise real.HarnessError(
                    f"resume did not create a fresh live invocation: {fresh!r}"
                )
            if (
                identity(owner_dir)["birth_branch"] != branch
                or worktree_for(repo, owner_dir) != worktree
            ):
                raise real.HarnessError("resume changed owner branch or worktree")
            same_owner = owner_for(repo) == owner_dir
            same_branch = identity(owner_dir)["birth_branch"] == branch
            same_worktree = worktree_for(repo, owner_dir) == worktree
            (worktree / "base-ci-fixed.txt").write_text(
                "base CI stabilized\n", encoding="utf-8"
            )
            real.git(
                worktree, "add", "base-ci-fixed.txt", "uncommitted-blocked-change.txt"
            )
            real.git(
                worktree,
                "commit",
                "-q",
                "-m",
                "Stabilize base CI and finish blocked task",
            )
            real.git(repo, "push", "-q", "origin", branch)
            owner_effects = EffectClient(restarted, role="dev", name=owner_dir.name)
            published = owner_effects.file_pr(
                title="Base CI stabilized",
                body="The scoped leaf did not introduce the failing base check.",
                base_branch="main",
            )
            published_after_resume = published.success and (
                repo / ".exo/published-heads.json"
            ).is_file()
            if not published_after_resume:
                raise real.HarnessError(
                    f"PR publication after resume failed: {published.raw!r}"
                )
            result = {
                "run": index,
                "blocked_event": {
                    "cause": payload["cause"],
                    "needs_human": payload["needs_human"],
                },
                "observed": {
                    "initial_invocation_running": initial.get("status") == "running",
                    "terminal_invocation_status": terminal.get("status"),
                    "parked_event_effect_succeeded": parked.success,
                    "same_owner": same_owner,
                    "same_branch": same_branch,
                    "same_worktree": same_worktree,
                    "human_gate_reused": gate_reused,
                    "resumed_invocation_running": fresh.get("status") == "running",
                    "pr_published_after_resume": published_after_resume,
                    "difficulty_scope": payload.get("scope_attribution"),
                },
                "negative_controls": {
                    "stale_resume_rejected": stale.success is False,
                    "not_exercised": [
                        "watcher-success",
                        "message-keyword",
                        "head-introduced-ci",
                    ],
                },
            }
            return result
        finally:
            if server is not None:
                stop_server_and_session(server, repo)
            real.stop_subprocess(mock, "blocked-handoff mock")
            if owner_dir is not None and owner_dir.exists():
                shutil.rmtree(owner_dir, ignore_errors=True)
            if worktree is not None and worktree.exists():
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree),
                    ],
                    check=False,
                )


def main() -> None:
    evidence = [run_case(index) for index in range(1, 4)]
    print(json.dumps({"runs": evidence}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
