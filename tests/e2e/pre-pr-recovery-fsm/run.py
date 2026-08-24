#!/usr/bin/env python3
"""Real-server recursive/parallel pre-publication recovery acceptance.

This is deliberately composed from the existing server-backed ordered probes:
the Rust server, generated development WASM, Unix MCP transport, disposable
Git remote, tmux panes, nested ledger queues, and Forgejo-shaped API all remain
inside the primary acceptance path. The harness adds one contract around that
path: recovery must preserve one owner and one dirty worktree while a sibling
continues, reconcile every restart boundary exactly once, and enter review only
after a real PR event.

The mutation smoke checks mutate only the captured evidence object. They prove
that each negative control has a live assertion without mutating production
source or pretending that a source copy is a real-server run.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ORDERED_DIR = PROJECT_ROOT / "tests/e2e/ordered-recursive"
sys.path.insert(0, str(ORDERED_DIR))

import real_server_transport as real  # noqa: E402

from tl_loop.state.store import RunStore  # noqa: E402


class HarnessError(RuntimeError):
    """The recursive recovery acceptance contract was violated."""


def _state(root: Path, run_id: str) -> Any:
    return RunStore(run_id, root / "controller-state").load()


def _nested_state(root: Path) -> Any:
    # The nested leaf is owned by the sub-a controller; the sibling ``nested``
    # sub-TL is an empty orchestration node and therefore has no slice rows.
    return RunStore(
        "sub-a",
        root / "lifecycle-state" / "recursive-root",
    ).load()


def _issue_id(result: Any) -> int:
    for candidate in real.json_objects(result.raw):
        for key in ("id", "number", "issue_id", "cicoIssueId"):
            value = candidate.get(key)
            if type(value) is int:
                return value
    raise HarnessError(f"disposable Chainlink issue had no integer ID: {result.raw!r}")


def _review_handoff_is_ordered(repo: Path, run_id: str) -> bool:
    """Require one UUID-scoped, identity-matched publication handoff.

    Forgejo requests, aggregate-opened events, and rows from another swarm
    are not evidence that this run entered review. The watcher emits
    pr.filed before its copilot.review and ci.status_changed rows, so the
    acceptance check consumes only that canonical event chain.
    """
    events = [
        event
        for event in real.server_ledger_events(repo)
        if event.get("run_id") == run_id and type(event.get("run_seq")) is int
    ]
    filed = [event for event in events if event.get("type") == "pr.filed"]
    reviews = [event for event in events if event.get("type") == "copilot.review"]
    ci = [event for event in events if event.get("type") == "ci.status_changed"]

    def identity(event: Mapping[str, object]) -> dict[str, object]:
        data = event.get("data")
        if not isinstance(data, Mapping):
            return {}
        return {
            key: data[key]
            for key in ("slice_id", "owner_id", "pr_number", "head_sha")
            if data.get(key) is not None
        }

    def matches(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
        left_id = identity(left)
        right_id = identity(right)
        shared = set(left_id) & set(right_id)
        return bool(shared) and all(left_id[key] == right_id[key] for key in shared)

    for filed_event in filed:
        filed_seq = filed_event["run_seq"]
        matching_reviews = [
            event
            for event in reviews
            if matches(filed_event, event) and event["run_seq"] > filed_seq
        ]
        matching_ci = [
            event
            for event in ci
            if matches(filed_event, event) and event["run_seq"] > filed_seq
        ]
        if matching_reviews and matching_ci:
            return True
    return False


def _validate_evidence(evidence: Mapping[str, object]) -> None:
    """Validate the machine-readable acceptance contract and its controls."""
    child_path = evidence.get("child_path")
    if (
        not isinstance(child_path, list)
        or len(child_path) != 4
        or child_path[:3] != ["root", "sub-a", "nested"]
        or not all(isinstance(item, str) and item for item in child_path)
    ):
        raise HarnessError(f"nested child path was not observed: {evidence!r}")
    phases = evidence.get("phases")
    if (
        not isinstance(phases, list)
        or len(phases) < 2
        or not all(isinstance(item, str) and item for item in phases)
        or phases[0] == phases[-1]
    ):
        raise HarnessError(f"recovery phases did not converge: {evidence!r}")
    lineage = evidence.get("recovery_lineage")
    if (
        not isinstance(lineage, list)
        or len(lineage) < 2
        or not all(isinstance(item, Mapping) for item in lineage)
    ):
        raise HarnessError(f"recovery lineage was not observed: {evidence!r}")
    attempt = evidence.get("slice_attempt")
    if type(attempt) is not int or attempt < 1:
        raise HarnessError(f"slice attempt was charged unexpectedly: {evidence!r}")
    generations = evidence.get("invocation_generations")
    if (
        not isinstance(generations, list)
        or len(generations) < 2
        or not all(type(item) is int and item >= 0 for item in generations)
        or generations != sorted(set(generations))
        or generations
        != [
            item["invocation_generation"]
            for item in lineage
            if type(item.get("invocation_generation")) is int
        ]
    ):
        raise HarnessError(
            f"recovery did not produce a unique authoritative lineage: {evidence!r}"
        )
    round_count = evidence.get("recovery_round")
    rounds = [
        item.get("recovery_round")
        for item in lineage
        if type(item.get("recovery_round")) is int
    ]
    if (
        type(round_count) is not int
        or round_count < 0
        or not rounds
        or round_count != max(rounds)
        or any(item.get("slice_attempt") != attempt for item in lineage)
    ):
        raise HarnessError(f"recovery round was not bounded: {evidence!r}")
    for key in (
        "unrelated_sibling_completed_while_waiting",
        "same_owner_worktree_preserved",
        "review_fsm_entered_only_after_pr_filed",
    ):
        if evidence.get(key) is not True:
            raise HarnessError(f"negative control {key} did not hold: {evidence!r}")


def _run_mutation_smoke(evidence: Mapping[str, object]) -> list[str]:
    """Ensure each required negative control fails for its intended reason."""
    mutations: dict[str, Any] = {
        "eager_gate": lambda value: value.update(
            {"review_fsm_entered_only_after_pr_filed": False}
        ),
        "duplicate_resume": lambda value: value.update(
            {"invocation_generations": [1, 2, 2]}
        ),
        "timeout_override": lambda value: value.update({"recovery_round": 99}),
        "parent_takeover": lambda value: value.update(
            {"same_owner_worktree_preserved": False}
        ),
        "scope_expansion": lambda value: value.update(
            {"child_path": ["root", "sub-a", "nested", "nested-leaf", "sibling"]}
        ),
        "no_op_recovery": lambda value: value.update(
            {"phases": [value["phases"][0], value["phases"][0]]}
        ),
    }
    failed: list[str] = []
    for name, mutate in mutations.items():
        candidate = copy.deepcopy(dict(evidence))
        mutate(candidate)
        try:
            _validate_evidence(candidate)
        except HarnessError:
            failed.append(name)
        else:
            raise HarnessError(f"negative mutant {name} passed unexpectedly")
    return failed


def _collect_evidence(
    root: Path,
    repo: Path,
    traces: Mapping[str, real.RecoveryTrace],
    marker: Path,
    swarm_id: str,
) -> dict[str, object]:
    nested = _nested_state(root)
    live = _state(root, "ordered-server-live")
    if nested.ledger_run_id is None:
        raise HarnessError("nested recovery state lost its shared ledger identity")
    if len(live.slices) != 2 or {
        state.status.value for state in live.slices.values()
    } != {"merged"}:
        raise HarnessError(f"sibling progress did not converge: {live.slices!r}")

    before = traces["aggregate_review"].records[0]
    after = traces["aggregate_review"].records[-1]
    before_slices = before.get("slices", {})
    after_slices = after.get("slices", {})
    sibling_progress = (
        isinstance(before_slices, Mapping)
        and len(before_slices) == 2
        and isinstance(after_slices, Mapping)
        and all(status in {"merged", "in_review"} for status in after_slices.values())
    )
    nested_leaf = next(
        (
            slice_id
            for slice_id, state in nested.slices.items()
            if getattr(state.status, "value", None) == "spawned"
        ),
        None,
    )
    if nested_leaf is None:
        raise HarnessError(
            f"nested leaf was not left at the live recovery boundary: {nested.slices!r}"
        )
    lineage = [
        snapshot
        for trace in traces.values()
        for record in trace.records
        for snapshot in [
            record.get("recovery", {}).get(nested_leaf)
            if isinstance(record.get("recovery"), Mapping)
            else None
        ]
        if isinstance(snapshot, Mapping)
    ]
    if len(lineage) < 2:
        raise HarnessError(
            "recovery trace did not contain two authoritative checkpoints for "
            f"{nested_leaf!r}: {traces!r}"
        )
    phases = [
        phase for snapshot in lineage if isinstance(phase := snapshot.get("phase"), str)
    ]
    generations = [
        generation
        for snapshot in lineage
        if type(generation := snapshot.get("invocation_generation")) is int
    ]
    rounds = [
        recovery_round
        for snapshot in lineage
        if type(recovery_round := snapshot.get("recovery_round")) is int
    ]
    owner_records = [
        (
            snapshot.get("owner_run_id"),
            snapshot.get("owner_agent_id"),
            snapshot.get("branch"),
            snapshot.get("worktree"),
        )
        for snapshot in lineage
    ]
    owner_preserved = (
        marker.read_bytes() == b"dirty recovery content\n"
        and all(
            all(isinstance(item, str) and item for item in record)
            for record in owner_records
        )
        and len(set(owner_records)) == 1
    )
    for run_id in (
        "ordered-server-dispatch-restart",
        "ordered-server-aggregate_review-restart",
        "ordered-server-base_revalidation-restart",
        "ordered-server-merging-restart",
    ):
        real.assert_action_journal_converged(
            root / "controller-state" / run_id / "action-journal.json"
        )
    evidence = {
        "child_path": ["root", "sub-a", "nested", nested_leaf],
        "phases": phases,
        "slice_attempt": nested.slices[nested_leaf].attempts,
        "invocation_generations": generations,
        "recovery_round": max(rounds, default=-1),
        "recovery_lineage": lineage,
        "unrelated_sibling_completed_while_waiting": sibling_progress,
        "same_owner_worktree_preserved": owner_preserved,
        "review_fsm_entered_only_after_pr_filed": _review_handoff_is_ordered(
            repo, swarm_id
        ),
        "restart_boundaries": sorted(traces),
        "observed_points": [
            record["point"]
            for record in traces["aggregate_review"].records
            if isinstance(record.get("point"), str)
        ],
        "nested_ledger_run_id": nested.ledger_run_id,
    }
    _validate_evidence(evidence)
    evidence["negative_controls"] = _run_mutation_smoke(evidence)
    return evidence


def run_case(index: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(
        prefix=f"exomonad-pre-pr-recovery-{index}-"
    ) as raw:
        root = Path(raw)
        repo, remote, _ = real.create_fixture(root)
        chainlink = repo / ".chainlink"
        chainlink.mkdir()
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
        issue_id: int | None = None
        marker = repo / ".exo/worktrees/parent/recovery-dirty.txt"
        try:
            server, client = real.start_server(root, repo, forgejo, PROJECT_ROOT)
            effects = real.EffectClient(client, role="tl", name="root")
            created = effects.chainlink_issue_create(
                title="Disposable recursive recovery acceptance",
                description="Closed by the acceptance harness after the run.",
                labels=("test",),
                priority="high",
            )
            if not created.success:
                raise HarnessError(
                    f"could not initialize disposable Chainlink DB: {created.raw!r}"
                )
            issue_id = _issue_id(created)
            swarm_id = real.server_run_id(repo)
            recovery_trace = real.run_recursive_checkpoint_probe(
                client, root, repo, swarm_id
            )
            real.run_real_watcher_routing_probe(client, root, repo, forgejo, swarm_id)
            marker.write_bytes(b"dirty recovery content\n")
            traces = real.run_delayed_restart_probe(client, root, repo, forgejo)
            traces["nested_recovery"] = recovery_trace
            real.run_live_ordered_probe(client, root, repo, swarm_id)
            real.assert_stage_events(repo, swarm_id)
            evidence = _collect_evidence(root, repo, traces, marker, swarm_id)
            result = {"run": index, "passed": True, **evidence}
            print(json.dumps(result, sort_keys=True))
            return result
        finally:
            if issue_id is not None:
                closed = effects.chainlink_issue_close(
                    issue_id=issue_id,
                    force=True,
                    summary="pre-PR recovery acceptance cleanup",
                )
                if not closed.success:
                    raise HarnessError(
                        f"disposable Chainlink issue cleanup failed: {closed.raw!r}"
                    )
            if server is not None:
                real.stop_subprocess(server, "pre-PR recovery server")
            subprocess.run(
                ["tmux", "kill-session", "-t", f"ordered-server-e2e-{os.getpid()}"],
                check=False,
                capture_output=True,
            )
            real.stop_subprocess(mock, "pre-PR recovery mock")


def main() -> None:
    results = [run_case(index) for index in range(1, 4)]
    if len(results) != 3 or not all(result.get("passed") is True for result in results):
        raise HarnessError(f"three-run acceptance did not converge: {results!r}")
    print(json.dumps({"passed": True, "runs": 3}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (HarnessError, KeyError, OSError, ValueError) as error:
        raise SystemExit(f"pre-publication recovery E2E failed: {error}") from error
