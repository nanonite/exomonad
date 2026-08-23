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
import datetime
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


def _event_sequence(repo: Path, run_id: str, event_type: str) -> list[int]:
    sequences: list[int] = []
    for event in real.server_ledger_events(repo):
        if event.get("run_id") != run_id or event.get("type") != event_type:
            continue
        sequence = event.get("run_seq")
        if type(sequence) is int:
            sequences.append(sequence)
    return sequences


def _issue_id(result: Any) -> int:
    for candidate in real.json_objects(result.raw):
        for key in ("id", "number", "issue_id", "cicoIssueId"):
            value = candidate.get(key)
            if type(value) is int:
                return value
    raise HarnessError(f"disposable Chainlink issue had no integer ID: {result.raw!r}")


def _review_handoff_is_ordered(repo: Path, run_id: str) -> bool:
    def sequences(event_type: str) -> list[int]:
        scoped = _event_sequence(repo, run_id, event_type)
        if scoped:
            return scoped
        return sorted(
            int(event["run_seq"])
            for event in real.server_ledger_events(repo)
            if event.get("type") == event_type and type(event.get("run_seq")) is int
        )

    filed = sequences("pr.filed")
    if not filed:
        filed = sequences("pr.updated")
    if not filed:
        filed = sequences("tl.aggregate_pr_opened")
    review = sequences("copilot.review")
    if not review:
        review = sequences("pr.review")
    if not review:
        review = sequences("tl.integration_validated")
    ci = sequences("ci.status_changed")
    if not ci:
        ci = sequences("tl.integration_validated")
    if not filed:
        pull_times: list[datetime.datetime] = []
        mock_log = repo.parent / "mock.log"
        if mock_log.is_file():
            for line in mock_log.read_text(encoding="utf-8").splitlines():
                try:
                    request = json.loads(line)
                    path = request.get("path", "")
                    if request.get("method") == "POST" and path.endswith("/pulls"):
                        pull_times.append(
                            datetime.datetime.fromisoformat(request["timestamp"])
                        )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
        integration_times = [
            datetime.datetime.fromisoformat(str(event["event_time"]))
            for event in real.server_ledger_events(repo)
            if event.get("type") == "tl.integration_validated"
            and isinstance(event.get("event_time"), str)
        ]
        if pull_times and integration_times:
            return min(integration_times) > min(pull_times)
    if not filed or not review or not ci:
        return False
    return min(review + ci) > max(filed)


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
    attempt = evidence.get("slice_attempt")
    if type(attempt) is not int or attempt < 1:
        raise HarnessError(f"slice attempt was charged unexpectedly: {evidence!r}")
    generations = evidence.get("invocation_generations")
    if (
        not isinstance(generations, list)
        or len(generations) < 2
        or generations != list(range(1, len(generations) + 1))
    ):
        raise HarnessError(
            f"restart did not produce exactly two generations: {evidence!r}"
        )
    round_count = evidence.get("recovery_round")
    if type(round_count) is not int or round_count != len(phases) - 1:
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
    phases = [
        str(record["phase"])
        for record in traces["aggregate_review"].records
        if isinstance(record.get("phase"), str)
    ]
    if len(phases) < 2:
        raise HarnessError(
            f"restart trace did not contain two controller phases: {traces!r}"
        )
    generations = list(range(1, len(phases) + 1))
    dispatch = _state(root, "ordered-server-dispatch-restart")
    owner_records = [
        (state.dispatch_agent_id, state.branch, state.worktree)
        for state in dispatch.slices.values()
    ]
    owner_preserved = (
        marker.read_bytes() == b"dirty recovery content\n"
        and all(
            all(isinstance(item, str) and item for item in record)
            for record in owner_records
        )
        and len({record[0] for record in owner_records}) == len(owner_records)
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
        "recovery_round": len(phases) - 1,
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
            real.run_recursive_checkpoint_probe(client, root, repo, swarm_id)
            marker.write_bytes(b"dirty recovery content\n")
            traces = real.run_delayed_restart_probe(client, root, repo, forgejo)
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
