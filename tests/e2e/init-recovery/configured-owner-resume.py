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

from tl_loop.client.effects import EffectClient
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


def wait_for_agent_event(path: Path, expected: str) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if path.is_file() and any(
            row.get("event") == expected for row in read_json_lines(path)
        ):
            return
        time.sleep(0.1)
    raise RuntimeError(f"worker did not record actual activity {expected!r}")


def verify_controller(repository: Path, baseline_path: Path, output: Path) -> None:
    """Verify recovery, publication and review came from the continued controller."""
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    root_dir = repository / ".exo/tl-loop/root"
    child_path = root_dir / "kani/run.json"
    owner_dir = Path(baseline["owner_dir"])
    agent_log = Path(os.environ["E2E_AGENT_LOG"])
    deadline = time.monotonic() + 180
    accepted: tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None = None
    while time.monotonic() < deadline:
        child = json.loads(child_path.read_text(encoding="utf-8"))
        leaf = child.get("slices", {}).get(OWNER_SLICE, {})
        try:
            current_invocation = invocation(owner_dir)
            publications = json.loads(
                (repository / ".exo/published-heads.json").read_text(encoding="utf-8")
            ).get("heads", [])
        except (OSError, json.JSONDecodeError):
            time.sleep(0.1)
            continue
        publication_matches = [
            row
            for row in publications
            if row.get("author_agent") == baseline["owner_agent_name"]
            and row.get("slice_id") == OWNER_SLICE
        ]
        publication = leaf.get("publication")
        handoff = leaf.get("handoff")
        publication_head = (
            publication.get("head_sha") if isinstance(publication, dict) else None
        )
        publication_pr = (
            publication.get("pr_number") if isinstance(publication, dict) else None
        )
        head_sha = publication_head
        successor_finished_successfully = current_invocation.get("status") not in {
            "failed",
            "killed",
        } and (
            current_invocation.get("status") != "exited"
            or (
                current_invocation.get("exit_code") == 0
                and current_invocation.get("exit_classification") == "one_shot_complete"
            )
        )
        if (
            current_invocation.get("invocation_id")
            != baseline["prior_invocation"]["invocation_id"]
            and successor_finished_successfully
            and leaf.get("status") in {"spawned", "in_review", "repairing", "merged"}
            and leaf.get("dispatch_agent_id") == baseline["owner_agent_name"]
            and leaf.get("dispatch_invocation_id")
            == current_invocation.get("invocation_id")
            and leaf.get("branch") == baseline["branch"]
            and Path(leaf.get("worktree", "")).resolve()
            == Path(baseline["worktree"]).resolve()
            and isinstance(leaf.get("pr_number"), int)
            and isinstance(head_sha, str)
            and len(publication_matches) == 1
            and publication_matches[0].get("pr_number") == leaf.get("pr_number")
            and publication_matches[0].get("head_sha") == head_sha
            and publication_pr == leaf.get("pr_number")
            and publication.get("head_branch") == baseline["branch"]
            and publication.get("base_branch") == "main.kani"
            and publication.get("invocation_id")
            == current_invocation.get("invocation_id")
            and (leaf.get("head_sha") is None or leaf.get("head_sha") == head_sha)
            and isinstance(handoff, dict)
            and handoff.get("pr_number") == leaf.get("pr_number")
            and handoff.get("head_sha") == head_sha
            and handoff.get("invocation_id") == current_invocation.get("invocation_id")
            and publication_matches[0].get("invocation_id")
            == current_invocation.get("invocation_id")
        ):
            reviewers = _reviewer_invocations(repository, leaf["pr_number"], head_sha)
            reviewer_approvals = [
                row
                for row in read_json_lines(agent_log)
                if row.get("event") == "reviewer_approved"
                and row.get("pr_number") == leaf["pr_number"]
                and row.get("head_sha") == head_sha
            ]
            if len(reviewers) == 1 and len(reviewer_approvals) == 1:
                accepted = (child, current_invocation, reviewers[0])
                break
        time.sleep(0.1)
    if accepted is None:
        raise RuntimeError(
            "production controller did not checkpoint the resumed #814 owner, adopt its PR, "
            "and launch the exact-head reviewer before timeout"
        )

    child, successor, reviewer = accepted
    leaf = child["slices"][OWNER_SLICE]
    publication = leaf["publication"]
    head_sha = publication["head_sha"]
    identity = json.loads((owner_dir / "identity.json").read_text(encoding="utf-8"))
    prior = baseline["prior_invocation"]
    if (
        successor.get("runtime") != "codex"
        or successor.get("model") != "gpt-6-luna"
        or successor.get("effort") != "xhigh"
        or successor.get("prior_invocation_id") != prior["invocation_id"]
        or successor.get("generation") != prior.get("generation", 1) + 1
        or identity.get("agent_type") != "codex"
        or identity.get("model") != "gpt-6-luna"
        or identity.get("effort") != "xhigh"
        or identity.get("agent_name") != baseline["owner_agent_name"]
        or identity.get("slice_id") != OWNER_SLICE
        or identity.get("birth_branch") != baseline["branch"]
        or Path(identity.get("working_dir", "")).resolve()
        != Path(baseline["worktree"]).resolve()
        or leaf.get("branch") != baseline["branch"]
        or Path(leaf.get("worktree", "")).resolve()
        != Path(baseline["worktree"]).resolve()
        or leaf.get("dispatch_agent_id") != baseline["owner_agent_name"]
        or leaf.get("dispatch_invocation_id") != successor.get("invocation_id")
        or leaf.get("status") in {"blocked", "pending"}
    ):
        raise RuntimeError(
            "controller checkpoint does not match the configured same-owner successor"
        )
    if retained_rejected_request(repository) != baseline["rejected_request"]:
        raise RuntimeError(
            "controller recovery changed the original rejected OpenCode spawn"
        )
    history = repository / ".exo/events" / f"{baseline['owner_agent_name']}.jsonl"
    history_lines = history.read_text(encoding="utf-8").splitlines()
    if history_lines[: len(baseline["history_lines"])] != baseline["history_lines"]:
        raise RuntimeError(
            "controller recovery rewrote historical OpenCode lifecycle records"
        )
    activity = event_counts(agent_log)
    for event in (
        "recovery_worker_started",
        "recovery_worker_filed_pr",
        "recovery_worker_notified_parent",
    ):
        if activity.get(event) != 1:
            raise RuntimeError(
                f"expected one real {event} activity, got {activity.get(event, 0)}"
            )
    reviewer_invocation = reviewer["invocation"]
    reviewer_approvals = [
        row
        for row in read_json_lines(agent_log)
        if row.get("event") == "reviewer_approved"
        and row.get("pr_number") == leaf["pr_number"]
        and row.get("head_sha") == head_sha
    ]
    if (
        reviewer_invocation.get("runtime") != "codex"
        or reviewer_invocation.get("role") != "reviewer"
        or reviewer_invocation.get("trigger") != "review"
        or reviewer_invocation.get("pr_number") != leaf["pr_number"]
        or reviewer_invocation.get("head_sha") != head_sha
        or len(reviewer_approvals) != 1
        or reviewer_approvals[0].get("agent_id") != reviewer["agent_name"]
        or reviewer_approvals[0].get("model") != "gpt-6.1-sol"
        or reviewer_approvals[0].get("effort") != "medium"
    ):
        raise RuntimeError(
            "controller reviewer is not bound to the configured exact PR head"
        )

    root = json.loads((root_dir / "run.json").read_text(encoding="utf-8"))
    root_kani = root.get("slices", {}).get("kani", {})
    if not root_kani.get("dispatch_agent_id") or not root_kani.get(
        "dispatch_invocation_id"
    ):
        raise RuntimeError(
            "parent RunStore lacks its Kani controller invocation projection"
        )
    original_journal = retained_rejected_request(repository)
    action_rows = json.loads(
        (root_dir / "kani/action-journal.json").read_text(encoding="utf-8")
    )
    resume_rows = [
        row
        for row in action_rows
        if row.get("operation") == "resume_blocked_leaf"
        and row.get("target") == OWNER_SLICE
        and row.get("status") == "confirmed"
    ]
    if len(resume_rows) != 1:
        raise RuntimeError(
            f"controller did not retain exactly one confirmed resume action: {resume_rows!r}"
        )
    ledger = canonical_events(repository)
    owner_invocations = [
        row
        for row in ledger
        if row.get("type") == "agent.invocation.started"
        and row.get("agent_id") == baseline["owner_agent_name"]
    ]
    if (
        len(owner_invocations) != 2
        or sum(
            row.get("data", {}).get("runtime") == "opencode"
            for row in owner_invocations
        )
        != 1
        or sum(
            row.get("data", {}).get("runtime") == "codex" for row in owner_invocations
        )
        != 1
    ):
        raise RuntimeError(
            f"owner history does not show exactly one OpenCode predecessor and Codex successor: {owner_invocations!r}"
        )
    lifecycle = [
        row
        for row in ledger
        if row.get("type")
        in {
            "tl.dispatch_intended",
            "tl.spawn_requested",
            "tl.spawn_request_accepted",
            "tl.dispatch_confirmed",
        }
        and isinstance(row.get("data"), dict)
        and row["data"].get("slice_id") == OWNER_SLICE
        and row["data"].get("dispatch_generation") == leaf.get("dispatch_generation")
    ]
    expected_events = {
        "tl.dispatch_intended",
        "tl.spawn_requested",
        "tl.spawn_request_accepted",
        "tl.dispatch_confirmed",
    }
    if {row.get("type") for row in lifecycle} != expected_events or len(lifecycle) != 4:
        raise RuntimeError(
            f"controller dispatch lifecycle is incomplete or duplicated: {lifecycle!r}"
        )

    record = {
        "owner_agent_name": baseline["owner_agent_name"],
        "prior_invocation_id": prior["invocation_id"],
        "rejected_request": original_journal,
        "successor_invocation_id": successor["invocation_id"],
        "branch": baseline["branch"],
        "worktree": baseline["worktree"],
        "runtime": successor["runtime"],
        "model": successor["model"],
        "effort": successor["effort"],
        "pr_number": leaf["pr_number"],
        "head_sha": head_sha,
        "reviewer_name": reviewer["agent_name"],
        "reviewer_invocation": reviewer_invocation,
        "reviewer_invocation_event_id": reviewer["event_id"],
        "successor_invocation_sha256": digest(owner_dir / "invocation.json"),
        "owner_identity_sha256": digest(owner_dir / "identity.json"),
        "owner_history_lines": history_lines,
        "leaf_status": leaf["status"],
        "leaf_dispatch_generation": leaf["dispatch_generation"],
        "lifecycle_event_ids": [row.get("event_id") for row in lifecycle],
        "agent_activity": activity,
    }
    output.write_text(
        json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(record, sort_keys=True))


def _reviewer_invocations(
    repository: Path, pr_number: int, head_sha: str
) -> list[dict[str, Any]]:
    reviewers: list[dict[str, Any]] = []
    for row in canonical_events(repository):
        value = row.get("data")
        if (
            row.get("type") == "agent.invocation.started"
            and isinstance(value, dict)
            and value.get("role") == "reviewer"
            and value.get("trigger") == "review"
            and value.get("pr_number") == pr_number
            and value.get("head_sha") == head_sha
        ):
            reviewers.append(
                {
                    "agent_name": row.get("agent_id"),
                    "event_id": row.get("event_id"),
                    "invocation": value,
                }
            )
    return reviewers


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
    if retained_rejected_request(repository) != evidence["rejected_request"]:
        raise RuntimeError("repeat init changed the original rejected Kani request")
    action_rows = json.loads(
        (repository / ".exo/tl-loop/root/kani/action-journal.json").read_text(
            encoding="utf-8"
        )
    )
    root_action_rows = json.loads(
        (repository / ".exo/tl-loop/root/action-journal.json").read_text(
            encoding="utf-8"
        )
    )
    rejected_controller_events = [
        row
        for row in [*action_rows, *root_action_rows]
        if row.get("operation") == "emit_controller_event"
        and row.get("status") == "rejected"
    ]
    if rejected_controller_events:
        raise RuntimeError(
            "repeat init retained rejected controller event calls: "
            f"{rejected_controller_events!r}"
        )
    resume_actions = [
        row
        for row in action_rows
        if row.get("operation") == "resume_blocked_leaf"
        and row.get("target") == OWNER_SLICE
        and row.get("status") == "confirmed"
    ]
    if (
        len(resume_actions) != 1
        or (resume_actions[0].get("result") or {})
        .get("result", {})
        .get("agent", {})
        .get("invocation_id")
        != evidence["successor_invocation_id"]
    ):
        raise RuntimeError(
            "repeat init did not retain exactly one successful resume for the same successor"
        )
    reviewer_actions = [
        row
        for row in action_rows
        if row.get("operation") == "spawn_reviewer"
        and row.get("target") == OWNER_SLICE
        and row.get("status") == "confirmed"
        and row.get("arguments", {}).get("pr_number") == evidence["pr_number"]
        and row.get("arguments", {}).get("head_sha") == evidence["head_sha"]
    ]
    if len(reviewer_actions) != 1 or (
        (reviewer_actions[0].get("result") or {}).get("result", {}).get("reviewer_name")
        != evidence["reviewer_name"]
    ):
        raise RuntimeError(
            "repeat init did not retain exactly one configured reviewer action for the same PR head"
        )
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
    reviewer_events = [
        row
        for row in canonical_events(repository)
        if row.get("type") == "agent.invocation.started"
        and row.get("agent_id") == evidence["reviewer_name"]
        and row.get("data", {}).get("role") == "reviewer"
        and row.get("data", {}).get("trigger") == "review"
        and row.get("data", {}).get("pr_number") == evidence["pr_number"]
        and row.get("data", {}).get("head_sha") == evidence["head_sha"]
    ]
    assert len(reviewer_events) == 1, (
        f"repeat init duplicated the reviewer invocation: {reviewer_events!r}"
    )
    assert (
        reviewer_events[0].get("event_id") == evidence["reviewer_invocation_event_id"]
    )
    assert digest(owner_dir / "identity.json") == evidence["owner_identity_sha256"]
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
    parser.add_argument("mode", choices=("seed", "verify-controller", "verify-repeat"))
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.mode == "seed":
        if arguments.output is None:
            parser.error("seed requires --output")
        seed(arguments.repo, arguments.output)
    elif arguments.mode == "verify-controller":
        if arguments.baseline is None or arguments.output is None:
            parser.error("verify-controller requires --baseline and --output")
        verify_controller(arguments.repo, arguments.baseline, arguments.output)
    else:
        if arguments.baseline is None:
            parser.error("verify-repeat requires --baseline")
        verify_repeat(arguments.repo, arguments.baseline)


if __name__ == "__main__":
    main()
