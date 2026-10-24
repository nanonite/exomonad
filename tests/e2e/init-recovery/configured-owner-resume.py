"""Seed and verify an OpenCode-to-Codex blocked-owner transition in Phase 8."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from tl_loop.client.effects import EffectClient, ToolResult
from tl_loop.client.transport import TransportClient

OWNER_SLICE = "issue-814-substitution-model-kani"
BLOCKED_ISSUE_ID = 814


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def worktree_fingerprint(worktree: Path) -> str:
    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    return "sha256:" + hashlib.sha256(status).hexdigest()


def owner_for(repository: Path) -> tuple[Path, dict[str, Any]]:
    owners = []
    for directory in (repository / ".exo/agents").iterdir():
        identity_path = directory / "identity.json"
        if not identity_path.is_file():
            continue
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        if identity.get("slug") == OWNER_SLICE:
            owners.append((directory, identity))
    if len(owners) != 1:
        raise RuntimeError(f"expected one #814 owner, found {len(owners)}")
    return owners[0]


def invocation(owner_dir: Path) -> dict[str, Any]:
    return json.loads((owner_dir / "invocation.json").read_text(encoding="utf-8"))


def retained_rejected_request(repository: Path) -> dict[str, Any]:
    journal = json.loads(
        (repository / ".exo/tl-loop/root/kani/action-journal.json").read_text(
            encoding="utf-8"
        )
    )
    matches = [
        row
        for row in journal
        if row.get("operation") == "spawn_leaf"
        and row.get("target") == OWNER_SLICE
        and row.get("status") == "rejected"
        and row.get("arguments", {}).get("intent_id")
        == "seed-issue-814-rejected-spawn-intent"
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one immutable original rejected Kani request, found {len(matches)}"
        )
    return matches[0]


def wait_for_terminal(owner_dir: Path) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current = invocation(owner_dir)
        if current.get("status") in {"exited", "failed", "killed"}:
            return current
        time.sleep(0.1)
    raise RuntimeError("historical OpenCode owner did not reach a terminal invocation")


def seed(repository: Path, output: Path) -> None:
    transport = TransportClient(project_root=repository, timeout=15)
    controller = EffectClient(transport, role="tl", name="kani")
    snapshot = json.loads(
        (repository / ".exo/tl-loop/plan.snapshot").read_text(encoding="utf-8")
    )
    declarations = snapshot["plan"]["sub_tls"]
    kani_declaration = next(row for row in declarations if row["name"] == "kani")
    leaves = kani_declaration["plan"]["leaves"]
    planned_task = next(row for row in leaves if row["name"] == OWNER_SLICE)["task"]
    if not isinstance(planned_task, str) or not planned_task.strip():
        raise RuntimeError(
            "accepted Kani declaration has no task for its historical owner"
        )
    spawned = controller.spawn_leaf(
        name=OWNER_SLICE,
        task=planned_task,
        intent_id="e2e-historical-opencode-814",
        agent_type="opencode",
        model="opencode-go/deepseek-v4.1-flash",
    )
    if spawned.success is not True:
        raise RuntimeError(f"historical OpenCode spawn failed: {spawned.raw!r}")
    deadline = time.monotonic() + 20
    owner_dir: Path | None = None
    identity: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            owner_dir, identity = owner_for(repository)
            break
        except (OSError, RuntimeError, json.JSONDecodeError):
            time.sleep(0.1)
    if owner_dir is None or identity is None:
        raise RuntimeError("spawn did not register the logical #814 owner")
    invocation_path = owner_dir / "invocation.json"
    original = wait_for_terminal(owner_dir)
    if (
        original.get("runtime") != "opencode"
        or original.get("model") != "opencode-go/deepseek-v4.1-flash"
        or original.get("effort") != "high"
        or original.get("exit_code") in (None, 0)
        or original.get("pr_number") is not None
        or original.get("head_sha") is not None
    ):
        raise RuntimeError(
            f"owner is not a failed pre-publication OpenCode invocation: {original!r}"
        )
    (owner_dir / "active_issue").write_text(f"{BLOCKED_ISSUE_ID}\n", encoding="utf-8")
    configured_worktree = Path(str(identity["working_dir"]))
    worktree = (
        configured_worktree
        if configured_worktree.is_absolute()
        else repository / configured_worktree
    )
    if not worktree.is_dir():
        raise RuntimeError("historical owner worktree is unavailable")
    fingerprint = worktree_fingerprint(worktree)
    history = repository / ".exo/events" / f"{identity['agent_name']}.jsonl"
    if not history.is_file():
        raise RuntimeError("historical OpenCode invocation event history is missing")
    record = {
        "owner_agent_name": identity["agent_name"],
        "owner_dir": str(owner_dir),
        "identity": identity,
        "prior_invocation": original,
        "task": planned_task,
        "rejected_request": retained_rejected_request(repository),
        "prior_invocation_file_sha256": digest(invocation_path),
        "branch": identity["birth_branch"],
        "worktree": str(worktree),
        "worktree_fingerprint": fingerprint,
        "history_sha256": digest(history),
        "history_lines": history.read_text(encoding="utf-8").splitlines(),
        "spawn_result_agent": (spawned.result or {}).get("agent"),
    }
    output.write_text(
        json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "seeded_owner": record["owner_agent_name"],
                "prior_invocation_id": original["invocation_id"],
            }
        )
    )


def resume(repository: Path, baseline_path: Path, output: Path) -> None:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    transport = TransportClient(project_root=repository, timeout=20)
    owner_dir, identity = owner_for(repository)
    if str(owner_dir) != baseline["owner_dir"]:
        raise RuntimeError("the #814 logical owner changed before resume")
    prior = invocation(owner_dir)
    if prior != baseline["prior_invocation"]:
        raise RuntimeError("the historical OpenCode invocation changed before resume")
    if retained_rejected_request(repository) != baseline["rejected_request"]:
        raise RuntimeError("the original rejected Kani request changed before resume")
    child = json.loads(
        (repository / ".exo/tl-loop/root/kani/run.json").read_text(encoding="utf-8")
    )
    leaf = child["slices"][OWNER_SLICE]
    if (
        leaf.get("status") != "blocked"
        or leaf.get("recovery", {}).get("phase") != "human_gate"
    ):
        raise RuntimeError("resume requires the durable active blocker projection")
    if leaf.get("pr_number") is not None or leaf.get("publication") is not None:
        raise RuntimeError("pre-publication recovery unexpectedly acquired PR evidence")

    request = {
        "chainlink_issue_id": BLOCKED_ISSUE_ID,
        "expected_invocation_id": prior["invocation_id"],
        "expected_branch": baseline["branch"],
        "expected_worktree_fingerprint": baseline["worktree_fingerprint"],
        "task": baseline["task"],
        "human_approved": True,
    }
    first = ToolResult.from_raw(
        transport.call_tool("tl", "kani", "resume_blocked_leaf", request)
    )
    if first.success is not True:
        raise RuntimeError(
            f"resume_blocked_leaf rejected the proven owner: {first.raw!r}"
        )
    deadline = time.monotonic() + 30
    successor: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        current = invocation(owner_dir)
        if current.get("invocation_id") != prior["invocation_id"]:
            successor = current
            break
        time.sleep(0.1)
    if successor is None:
        raise RuntimeError(
            "resume_blocked_leaf did not register its successor invocation"
        )
    current_identity = json.loads(
        (owner_dir / "identity.json").read_text(encoding="utf-8")
    )
    if (
        successor.get("runtime") != "codex"
        or successor.get("model") != "gpt-6-luna"
        or successor.get("effort") != "xhigh"
        or successor.get("prior_invocation_id") != prior["invocation_id"]
        or successor.get("generation") != prior.get("generation", 1) + 1
        or current_identity.get("agent_type") != "codex"
        or current_identity.get("model") != "gpt-6-luna"
        or current_identity.get("effort") != "xhigh"
        or current_identity.get("agent_name") != baseline["identity"]["agent_name"]
        or current_identity.get("slice_id") != baseline["identity"]["slice_id"]
        or current_identity.get("birth_branch") != baseline["branch"]
        or current_identity.get("working_dir") != baseline["identity"]["working_dir"]
    ):
        raise RuntimeError(
            f"Codex successor did not preserve owner lineage: {successor!r} {current_identity!r}"
        )

    # The process log is deliberately outside the disposable repository.
    agent_log = Path(os.environ["E2E_AGENT_LOG"])
    wait_for_agent_event(agent_log, "recovery_worker_filed_pr")
    wait_for_agent_event(agent_log, "recovery_worker_notified_parent")
    history = repository / ".exo/events" / f"{identity['agent_name']}.jsonl"
    history_lines = history.read_text(encoding="utf-8").splitlines()
    if history_lines[: len(baseline["history_lines"])] != baseline["history_lines"]:
        raise RuntimeError("resume rewrote the historical OpenCode lifecycle records")
    switch_events = [
        row
        for row in canonical_events(repository)
        if row.get("type") == "agent.harness_switch"
    ]
    if not any(
        row.get("agent_id") == "kani"
        and row.get("data", {}).get("to_harness") == "codex"
        and row.get("data", {}).get("to_model") == "gpt-6-luna"
        and row.get("data", {}).get("to_effort") == "xhigh"
        for row in switch_events
    ):
        raise RuntimeError(
            "configured OpenCode-to-Codex switch has no durable audit evidence"
        )

    publication_rows = json.loads(
        (repository / ".exo/published-heads.json").read_text(encoding="utf-8")
    )["heads"]
    publication = next(
        (
            row
            for row in publication_rows
            if row.get("author_agent") == identity["agent_name"]
            and row.get("slice_id") == OWNER_SLICE
        ),
        None,
    )
    if (
        not isinstance(publication, dict)
        or not isinstance(publication.get("pr_number"), int)
        or not publication.get("head_sha")
    ):
        raise RuntimeError(
            "Codex successor did not publish one authoritative PR/head binding"
        )
    pr_number, head_sha = publication["pr_number"], publication["head_sha"]
    reviewer = EffectClient(transport, role="tl", name="root").spawn_reviewer(
        pr_number=pr_number,
        head_sha=head_sha,
        acceptance_criteria=["Confirm the resumed owner's exact PR head."],
        force=False,
    )
    if reviewer.success is not True:
        raise RuntimeError(f"configured reviewer spawn failed: {reviewer.raw!r}")
    reviewer_name = str((reviewer.result or {}).get("reviewer_name") or "")
    reviewer_dir = repository / ".exo/agents" / reviewer_name
    reviewer_identity = json.loads(
        (reviewer_dir / "identity.json").read_text(encoding="utf-8")
    )
    reviewer_invocation = json.loads(
        (reviewer_dir / "invocation.json").read_text(encoding="utf-8")
    )
    if (
        reviewer_invocation.get("runtime") != "codex"
        or reviewer_invocation.get("model") != "gpt-6.1-sol"
        or reviewer_invocation.get("effort") != "medium"
        or reviewer_identity.get("agent_type") != "codex"
        or reviewer_identity.get("model") != "gpt-6.1-sol"
        or reviewer_identity.get("effort") != "medium"
        or reviewer_invocation.get("pr_number") != pr_number
        or reviewer_invocation.get("head_sha") != head_sha
    ):
        raise RuntimeError("reviewer did not launch on the configured exact PR head")

    successor_bytes = (owner_dir / "invocation.json").read_bytes()
    history_bytes = history.read_bytes()
    repeat = ToolResult.from_raw(
        transport.call_tool("tl", "kani", "resume_blocked_leaf", request)
    )
    if repeat.success is not True:
        raise RuntimeError(
            f"same-request repeat did not adopt the successor: {repeat.raw!r}"
        )
    if (
        owner_dir / "invocation.json"
    ).read_bytes() != successor_bytes or history.read_bytes() != history_bytes:
        raise RuntimeError(
            "same-request repeat duplicated successor invocation or owner events"
        )
    record = {
        "owner_agent_name": identity["agent_name"],
        "prior_invocation_id": prior["invocation_id"],
        "rejected_request": baseline["rejected_request"],
        "successor_invocation_id": successor["invocation_id"],
        "branch": baseline["branch"],
        "worktree": str(baseline["worktree"]),
        "runtime": successor["runtime"],
        "model": successor["model"],
        "effort": successor["effort"],
        "pr_number": pr_number,
        "head_sha": head_sha,
        "reviewer_name": reviewer_name,
        "reviewer_runtime": reviewer_invocation["runtime"],
        "reviewer_model": reviewer_invocation["model"],
        "reviewer_effort": reviewer_invocation["effort"],
        "reviewer_invocation": reviewer_invocation,
        "successor_invocation_sha256": digest(owner_dir / "invocation.json"),
        "owner_history_sha256": digest(history),
        "owner_history_lines": history.read_text(encoding="utf-8").splitlines(),
        "reviewer_identity_sha256": digest(reviewer_dir / "identity.json"),
        "agent_activity": event_counts(agent_log),
        "remote_pull_publications": sum(
            row.get("method") == "POST"
            and str(row.get("path", ""))
            .split("?", 1)[0]
            .endswith("/api/v1/repos/owner/repo/pulls")
            for row in read_json_lines(Path(os.environ["E2E_MOCK_LOG"]))
        ),
    }
    output.write_text(
        json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(record, sort_keys=True))


def wait_for_agent_event(path: Path, expected: str) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if path.is_file() and any(
            row.get("event") == expected for row in read_json_lines(path)
        ):
            return
        time.sleep(0.1)
    raise RuntimeError(f"worker did not record actual activity {expected!r}")


def read_json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def event_counts(path: Path) -> dict[str, int]:
    rows = read_json_lines(path)
    return {
        name: sum(row.get("event") == name for row in rows)
        for name in sorted({row.get("event") for row in rows})
    }


def canonical_events(repository: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for segment in sorted(
            (repository / ".exo/ledger/segments").glob("segment-*.jsonl")
        )
        for line in segment.read_text(encoding="utf-8").splitlines()
    ]


def verify_repeat(repository: Path, evidence_path: Path) -> None:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    owner_dir, _ = owner_for(repository)
    reviewer_dir = repository / ".exo/agents" / evidence["reviewer_name"]
    if retained_rejected_request(repository) != evidence["rejected_request"]:
        raise RuntimeError("repeat init changed the original rejected Kani request")
    assert (
        digest(owner_dir / "invocation.json") == evidence["successor_invocation_sha256"]
    )
    history = repository / ".exo/events" / f"{evidence['owner_agent_name']}.jsonl"
    history_lines = history.read_text(encoding="utf-8").splitlines()
    prior_history = evidence["owner_history_lines"]
    assert history_lines[: len(prior_history)] == prior_history, (
        "repeat init rewrote or removed historical owner events"
    )
    additions = history_lines[len(prior_history) :]
    assert len(additions) <= 1, (
        f"repeat init appended unexpected owner events: {additions!r}"
    )
    if additions:
        delivery = json.loads(additions[0])
        data = delivery.get("data")
        assert delivery.get("type") == "agent.guidance.delivery"
        assert isinstance(data, dict)
        assert data.get("invocation_id") == evidence["successor_invocation_id"]
        assert data.get("outcome") == "success"
        assert data.get("channel") == "tmux_injection"
        evidence["owner_history_lines"] = history_lines
        evidence["owner_history_sha256"] = digest(history)
    current_reviewer = json.loads(
        (reviewer_dir / "invocation.json").read_text(encoding="utf-8")
    )
    for field in (
        "invocation_id",
        "generation",
        "runtime",
        "model",
        "effort",
        "pr_number",
        "head_sha",
        "trigger",
    ):
        assert current_reviewer.get(field) == evidence["reviewer_invocation"].get(
            field
        ), f"repeat init changed reviewer {field}"
    assert (
        digest(reviewer_dir / "identity.json") == evidence["reviewer_identity_sha256"]
    )
    assert (
        len(
            [
                row
                for row in json.loads(
                    (repository / ".exo/published-heads.json").read_text(
                        encoding="utf-8"
                    )
                )["heads"]
                if row.get("author_agent") == evidence["owner_agent_name"]
                and row.get("slice_id") == OWNER_SLICE
            ]
        )
        == 1
    ), "repeat init duplicated the resumed owner's PR publication"
    assert (
        len(
            [
                row
                for row in read_json_lines(Path(os.environ["E2E_AGENT_LOG"]))
                if row.get("event") == "recovery_worker_filed_pr"
            ]
        )
        == 1
    ), "repeat init launched a duplicate resumed worker"
    assert (
        len(
            [
                row
                for row in read_json_lines(Path(os.environ["E2E_AGENT_LOG"]))
                if row.get("event") == "reviewer_approved"
                and row.get("pr_number") == evidence["pr_number"]
                and row.get("head_sha") == evidence["head_sha"]
            ]
        )
        == 1
    ), "repeat init duplicated review activity"
    events = canonical_events(repository)
    owner_invocations = [
        row
        for row in events
        if row.get("type") == "agent.invocation.started"
        and row.get("agent_id") == evidence["owner_agent_name"]
    ]
    assert len(owner_invocations) == 2, (
        f"expected one OpenCode and one Codex invocation, got {len(owner_invocations)}"
    )
    reviewer_invocations = [
        row
        for row in events
        if row.get("type") == "agent.invocation.started"
        and row.get("agent_id") == evidence["reviewer_name"]
    ]
    assert len(reviewer_invocations) == 1, (
        f"repeat init created another reviewer invocation: {reviewer_invocations!r}"
    )
    assert (
        sum(
            row.get("data", {}).get("runtime") == "opencode"
            for row in owner_invocations
        )
        == 1
    )
    assert (
        sum(row.get("data", {}).get("runtime") == "codex" for row in owner_invocations)
        == 1
    )
    activity = event_counts(Path(os.environ["E2E_AGENT_LOG"]))
    assert activity.get("historical_opencode_owner_failed") == 1
    assert activity.get("recovery_worker_started") == 1
    assert activity.get("recovery_worker_filed_pr") == 1
    assert activity.get("recovery_worker_notified_parent") == 1
    evidence_path.write_text(
        json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "repeat init retained one OpenCode predecessor, one Codex successor, and one exact-head reviewer"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("seed", "resume", "verify-repeat"))
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.mode == "seed":
        if arguments.output is None:
            parser.error("seed requires --output")
        seed(arguments.repo, arguments.output)
    elif arguments.mode == "resume":
        if arguments.baseline is None or arguments.output is None:
            parser.error("resume requires --baseline and --output")
        resume(arguments.repo, arguments.baseline, arguments.output)
    else:
        if arguments.baseline is None:
            parser.error("verify-repeat requires --baseline")
        verify_repeat(arguments.repo, arguments.baseline)


if __name__ == "__main__":
    main()
