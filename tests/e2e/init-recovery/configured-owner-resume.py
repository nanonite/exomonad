"""Seed and verify an OpenCode-to-Codex blocked-owner transition in Phase 8."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
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


def retained_original_dispatch(
    repository: Path, baseline: dict[str, Any]
) -> dict[str, Any]:
    """Return the exact immutable owner launch record for either fixture shape."""
    if baseline.get("historical_dispatch_kind") != "accepted_terminal_provider_failure":
        return retained_rejected_request(repository)
    expected = baseline.get("accepted_spawn_journal")
    if not isinstance(expected, dict):
        raise TypeError(
            "terminal-provider baseline lacks its accepted spawn journal record"
        )
    rows = json.loads(
        (repository / ".exo/tl-loop/root/kani/action-journal.json").read_text(
            encoding="utf-8"
        )
    )
    intent_id = expected.get("arguments", {}).get("intent_id")
    matches = [
        row
        for row in rows
        if row.get("operation") == "spawn_leaf"
        and row.get("target") == OWNER_SLICE
        and row.get("status") == "confirmed"
        and row.get("arguments", {}).get("intent_id") == intent_id
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one accepted historical owner launch, found {len(matches)}"
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


def seed_terminal_provider_failure(repository: Path, output: Path) -> None:
    """Create a real accepted OpenCode launch, terminal failure, and blocked event."""
    from tl_loop.fsm.child import ChildKind, ChildRecord
    from tl_loop.fsm.scope import TLRunning
    from tl_loop.loop.driver import EffectIntent
    from tl_loop.loop.journal import EffectJournal
    from tl_loop.state.schema import SliceStatus
    from tl_loop.state.store import RunStore

    transport = TransportClient(project_root=repository, timeout=30)
    controller = EffectClient(transport, role="tl", name="kani")
    snapshot = json.loads(
        (repository / ".exo/tl-loop/plan.snapshot").read_text(encoding="utf-8")
    )
    kani_declaration = next(
        row for row in snapshot["plan"]["sub_tls"] if row["name"] == "kani"
    )
    leaf_declaration = next(
        row for row in kani_declaration["plan"]["leaves"] if row["name"] == OWNER_SLICE
    )
    task = leaf_declaration["task"]
    if leaf_declaration.get("chainlink_issue_id") != BLOCKED_ISSUE_ID:
        raise RuntimeError(
            "accepted terminal-failure fixture lacks the typed #814 binding"
        )
    intent_id = "e2e-terminal-opencode-814"
    arguments: dict[str, Any] = {
        "name": OWNER_SLICE,
        "task": task,
        "intent_id": intent_id,
        "agent_type": "opencode",
        "model": "opencode-go/deepseek-v4.1-flash",
        "boundary": leaf_declaration.get("boundary", []),
        "verify": leaf_declaration.get("verify", []),
    }
    spawn_intent = EffectIntent("spawn_leaf", OWNER_SLICE, arguments, True)
    child_store = RunStore("kani", repository / ".exo/tl-loop/root")
    journal = EffectJournal("kani", child_store.run_dir / "action-journal.json")
    journal.append(spawn_intent)
    spawned = controller.spawn_leaf(
        name=OWNER_SLICE,
        task=task,
        intent_id=intent_id,
        agent_type="opencode",
        model="opencode-go/deepseek-v4.1-flash",
        boundary=leaf_declaration.get("boundary"),
        verify=leaf_declaration.get("verify"),
    )
    journal.mark_result(spawn_intent, spawned)
    if spawned.success is not True:
        raise RuntimeError(
            f"actual OpenCode host launch was refused: {spawned.error!r}"
        )

    owner_dir, identity = owner_for(repository)
    original = wait_for_terminal(owner_dir)
    if (
        identity.get("agent_type") != "opencode"
        or identity.get("model") != "opencode-go/deepseek-v4.1-flash"
        or identity.get("effort") != "high"
        or identity.get("slice_id") != OWNER_SLICE
        or identity.get("birth_branch") != original.get("branch")
        or original.get("runtime") != "opencode"
        or original.get("model") != "opencode-go/deepseek-v4.1-flash"
        or original.get("effort") != "high"
        or original.get("trigger") != "spawn"
        or original.get("generation") != 1
        or original.get("status") not in {"failed", "exited"}
        or type(original.get("exit_code")) is not int
        or original.get("exit_code") == 0
        or original.get("exit_classification") != "nonzero_exit"
        or not isinstance(original.get("ended_at"), (float, int))
        or "insufficient account funds"
        not in str(original.get("stderr_tail", "")).casefold()
        or original.get("pr_number") is not None
        or original.get("head_sha") is not None
        or owner_for_issue_id(owner_dir) != BLOCKED_ISSUE_ID
    ):
        raise RuntimeError(
            f"OpenCode invocation did not end in the accepted provider failure: {original!r}"
        )

    invocation_id = original["invocation_id"]
    branch = identity["birth_branch"]
    worktree_recorded = identity["working_dir"]
    worktree = Path(worktree_recorded)
    if not worktree.is_absolute():
        worktree = repository / worktree
    worktree = worktree.resolve(strict=True)
    events = canonical_events(repository)
    runtime_run_id = (repository / ".exo/run_id").read_text(encoding="utf-8").strip()
    accepted_rows = [
        row
        for row in events
        if row.get("type") == "agent.spawned"
        and row.get("agent_id") == "kani"
        and row.get("run_id") == runtime_run_id
        and row.get("invocation_id") == invocation_id
        and row.get("data", {}).get("intent_id") == intent_id
        and row.get("data", {}).get("child_agent") == identity.get("agent_name")
        and row.get("data", {}).get("branch") == branch
    ]
    if len(accepted_rows) != 1:
        raise RuntimeError(
            f"accepted OpenCode launch has {len(accepted_rows)} matching authoritative spawn events"
        )
    spawn_event = accepted_rows[0]
    spawn_seq = spawn_event.get("run_seq")
    if type(spawn_seq) is not int or spawn_seq <= 0:
        raise RuntimeError("accepted OpenCode spawn event has no durable sequence")

    state = child_store.load()
    manifest = state.plan_manifest
    if manifest is None:
        raise RuntimeError("Kani checkpoint has no accepted child manifest")
    current = state.slices.get(OWNER_SLICE)
    if current is None or current.status is not SliceStatus.PENDING:
        raise RuntimeError(
            "terminal-failure fixture must begin with an undispatched accepted leaf"
        )
    node = next((item for item in manifest.nodes if item.name == OWNER_SLICE), None)
    if node is None or node.chainlink_issue_id != BLOCKED_ISSUE_ID:
        raise RuntimeError(
            "accepted child manifest does not bind the terminal leaf to issue #814"
        )
    requested_harness = str(leaf_declaration.get("agent_type", ""))
    launched = replace(
        current,
        status=SliceStatus.SPAWNED,
        attempts=1,
        branch=branch,
        base_ref="main.kani",
        agent_type="opencode",
        model="opencode-go/deepseek-v4.1-flash",
        requested_harness=requested_harness,
        resolved_harness="opencode/opencode-go/deepseek-v4.1-flash",
        dispatch_intent_id=intent_id,
        dispatch_started_at=float(original["started_at"]),
        dispatch_generation=2,
        dispatch_ledger_floor=spawn_seq - 1,
        dispatch_last_boundary="agent.spawned",
        dispatch_agent_id=identity["agent_name"],
        dispatch_invocation_id=invocation_id,
        dispatch_authoritative_event_seq=spawn_seq,
        chainlink_issue_id=BLOCKED_ISSUE_ID,
        worktree=str(current.worktree),
        recovery=None,
        park_cause=None,
    )
    accepted_child = ChildRecord(
        OWNER_SLICE,
        ChildKind.LEAF,
        dispatch_intent_id=intent_id,
        invocation_id=invocation_id,
        manifest_node_id=node.node_id,
        manifest_revision=manifest.manifest_revision,
    )
    child_running = TLRunning(
        current_order=0,
        pending_by_order={},
        scope_path=(manifest.scope_id,),
        plan_digest=manifest.digest,
        parallel_pending=(accepted_child,),
    )
    child_store.checkpoint(
        child_running,
        {**state.slices, OWNER_SLICE: launched},
        state.budgets,
        spawn_seq,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        plan_manifest=manifest,
    )

    root_store = RunStore("root", repository / ".exo/tl-loop")
    root_state = root_store.load()
    root_store.checkpoint(
        root_state.recursive_fsm,
        root_state.slices,
        root_state.budgets,
        spawn_seq,
        current_order=root_state.current_order,
        ordered_stages=root_state.ordered_stages,
        integration=root_state.integration,
        plan_manifest=root_state.plan_manifest,
    )
    blocker = controller.emit_controller_event(
        event_type="agent.task_blocked",
        payload={
            "outcome": "blocked",
            "slice_id": OWNER_SLICE,
            "cause": "human_decision_required",
            "scope_attribution": "agent_lifecycle",
            "needs_human": True,
            "retryable": True,
            "recovery_action": "apply the configured worker route to the preserved assignment",
            "declared_difficulty": "standard",
            "matched_difficulty_rule": "missing_handoff:nonzero_exit",
            "attempt": 1,
        },
    )
    if blocker.success is not True or not isinstance(blocker.result, dict):
        raise RuntimeError(
            f"production task-blocked event was not recorded: {blocker.error!r}"
        )
    blocker_id = blocker.result.get("event_id")
    blocker_rows = [
        row for row in canonical_events(repository) if row.get("event_id") == blocker_id
    ]
    if len(blocker_rows) != 1 or blocker_rows[0].get("type") != "agent.task_blocked":
        raise RuntimeError(
            "post-launch human blocker is not retained in the canonical ledger"
        )
    if blocker_rows[0].get("run_seq", 0) <= spawn_seq:
        raise RuntimeError(
            "terminal blocker does not follow the accepted OpenCode spawn"
        )

    history = repository / ".exo/events" / f"{identity['agent_name']}.jsonl"
    output.write_text(
        json.dumps(
            {
                "historical_dispatch_kind": "accepted_terminal_provider_failure",
                "owner_agent_name": identity["agent_name"],
                "owner_dir": str(owner_dir),
                "prior_invocation": original,
                "prior_invocation_file_sha256": digest(owner_dir / "invocation.json"),
                "branch": branch,
                "worktree": str(worktree),
                "planned_worktree": current.worktree,
                "worktree_fingerprint": worktree_fingerprint(worktree),
                "spawn_event_id": spawn_event["event_id"],
                "spawn_event_seq": spawn_seq,
                "accepted_spawn_event": spawn_event,
                "blocker_event_id": blocker_id,
                "blocker_event_seq": blocker_rows[0]["run_seq"],
                "accepted_spawn_journal": journal.entries_for(
                    "spawn_leaf", OWNER_SLICE
                )[0],
                "history_lines": history.read_text(encoding="utf-8").splitlines(),
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"accepted_spawn": invocation_id, "terminal_status": original["status"]}
        )
    )


def owner_for_issue_id(owner_dir: Path) -> int | None:
    try:
        return int((owner_dir / "active_issue").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


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
    if baseline.get("historical_dispatch_kind") == "accepted_terminal_provider_failure":
        if (
            baseline.get("prior_invocation", {}).get("status")
            not in {"failed", "exited"}
            or "insufficient account funds"
            not in str(
                baseline.get("prior_invocation", {}).get("stderr_tail", "")
            ).casefold()
        ):
            raise RuntimeError(
                "terminal-provider baseline does not retain the exact OpenCode failure"
            )
        matching_spawns = [
            row
            for row in canonical_events(repository)
            if row.get("event_id") == baseline.get("spawn_event_id")
        ]
        if matching_spawns != [baseline.get("accepted_spawn_event")]:
            raise RuntimeError(
                "accepted OpenCode spawn ledger evidence changed after launch"
            )
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
    expected_original = baseline.get(
        "accepted_spawn_journal", baseline.get("rejected_request")
    )
    if retained_original_dispatch(repository, baseline) != expected_original:
        raise RuntimeError(
            "controller recovery changed the original OpenCode dispatch record"
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
    original_journal = retained_original_dispatch(repository, baseline)
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
        "historical_dispatch_kind": baseline.get("historical_dispatch_kind"),
        "accepted_spawn_journal": baseline.get("accepted_spawn_journal"),
        "accepted_spawn_event": baseline.get("accepted_spawn_event"),
        "spawn_event_id": baseline.get("spawn_event_id"),
        "prior_invocation": baseline["prior_invocation"],
        "owner_agent_name": baseline["owner_agent_name"],
        "prior_invocation_id": prior["invocation_id"],
        "original_dispatch_journal": original_journal,
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
        "successor_invocation_generation": successor["generation"],
        "owner_identity_sha256": digest(owner_dir / "identity.json"),
        "owner_history_lines": history_lines,
        "leaf_status": leaf["status"],
        "leaf_dispatch_generation": leaf["dispatch_generation"],
        "lifecycle_event_ids": [row.get("event_id") for row in lifecycle],
        "agent_activity": activity,
    }
    if baseline.get("historical_dispatch_kind") != "accepted_terminal_provider_failure":
        record["rejected_request"] = original_journal
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
    retained_launch_event = [
        row
        for row in canonical_events(repository)
        if row.get("event_id") == evidence.get("spawn_event_id")
    ]
    if evidence.get(
        "historical_dispatch_kind"
    ) == "accepted_terminal_provider_failure" and retained_launch_event != [
        evidence.get("accepted_spawn_event")
    ]:
        raise RuntimeError(
            "repeat init changed the accepted OpenCode launch ledger row"
        )
    owner_dir, _ = owner_for(repository)
    current_invocation = invocation(owner_dir)
    expected_successor_generation = evidence.get("successor_invocation_generation")
    if type(expected_successor_generation) is not int:
        expected_successor_generation = (
            int(evidence.get("prior_invocation", {}).get("generation", 1)) + 1
        )
    if retained_original_dispatch(repository, evidence) != evidence.get(
        "accepted_spawn_journal", evidence.get("rejected_request")
    ):
        raise RuntimeError("repeat init changed the original Kani dispatch record")
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
    if (
        current_invocation.get("invocation_id") != evidence["successor_invocation_id"]
        or current_invocation.get("runtime") != evidence["runtime"]
        or current_invocation.get("model") != evidence["model"]
        or current_invocation.get("effort") != evidence["effort"]
        or current_invocation.get("prior_invocation_id")
        != evidence["prior_invocation_id"]
        or current_invocation.get("generation") != expected_successor_generation
        or current_invocation.get("runtime_agent_id") != evidence["owner_agent_name"]
        or current_invocation.get("slice_id") != OWNER_SLICE
        or current_invocation.get("branch") != evidence["branch"]
        or Path(current_invocation.get("worktree", "")).resolve()
        != Path(evidence["worktree"]).resolve()
        or current_invocation.get("status") != "exited"
        or current_invocation.get("exit_code") != 0
        or current_invocation.get("exit_classification") != "one_shot_complete"
    ):
        raise RuntimeError(
            "repeat init replaced or corrupted the completed configured successor invocation"
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
    parser.add_argument(
        "mode",
        choices=("seed", "seed-terminal", "verify-controller", "verify-repeat"),
    )
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.mode == "seed":
        if arguments.output is None:
            parser.error("seed requires --output")
        seed(arguments.repo, arguments.output)
    elif arguments.mode == "seed-terminal":
        if arguments.output is None:
            parser.error("seed-terminal requires --output")
        seed_terminal_provider_failure(arguments.repo, arguments.output)
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
