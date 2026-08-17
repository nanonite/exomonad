#!/usr/bin/env python3
"""Run the first active TL loop wave against a scratch Git repository."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import MutationBlocked
from tl_loop.client.transport import JsonObject
from tl_loop.events.queue import LedgerQueue
from tl_loop.events.reader import LedgerReader, SequenceStatus
from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.driver import TLLoopConfig, TLRunResult, WorkPlan, tl_run
from tl_loop.select.capability import CapabilityMap
from tl_loop.select.classify import Difficulty
from tl_loop.select.ledger import reconcile
from tl_loop.select.policy import validate_policy
from tl_loop.state.store import RunStore

RUN_ID = "active-wave"
SESSION = "e2e-tl-loop-active"
ACTIVE_EVENT_TIME = datetime.now(UTC).isoformat()
LEAVES = ("active-slice-a", "active-slice-b")
PR_NUMBERS = {name: 1001 + index for index, name in enumerate(LEAVES)}
REVIEW_SHAS = {name: f"review-{name}" for name in LEAVES}
ACTUAL_TOKENS = {name: 600 for name in LEAVES}


@dataclass
class PullRequest:
    """A scratch-remote PR record produced by the deterministic leaf stub."""

    number: int
    branch: str
    worktree: Path
    review_sha: str
    commit_sha: str
    merged: bool = False


@dataclass
class ActiveEffectTransport:
    """Execute real scratch-repository effects behind the controller boundary."""

    repo: Path
    remote: Path
    ledger_segments: Path
    calls: list[dict[str, object]] = field(default_factory=list)
    prs: dict[int, PullRequest] = field(default_factory=dict)
    adjudications: int = 0
    controller_events: list[dict[str, object]] = field(default_factory=list)

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        """Record and execute one active effect without an interactive agent."""
        del role, name
        self.calls.append(
            {"tool": tool_name, "arguments": json.loads(json.dumps(arguments))}
        )
        if tool_name == "emit_controller_event":
            return self._emit_controller_event(arguments)
        if tool_name == "spawn_leaf":
            return self._spawn_leaf(arguments)
        if tool_name == "watcher_pr_state":
            return self._watcher_pr_state(arguments)
        if tool_name == "merge_pr":
            return self._merge_pr(arguments)
        raise AssertionError(f"unexpected active-loop effect: {tool_name}")

    def _emit_controller_event(self, arguments: JsonObject) -> JsonObject:
        event_type = _string(arguments, "event_type")
        payload = arguments.get("payload")
        if not isinstance(payload, dict):
            raise TypeError("controller event payload must be an object")
        event = {"event_type": event_type, "payload": json.loads(json.dumps(payload))}
        self.controller_events.append(event)
        if event_type in {
            "tl.dispatch_intended",
            "tl.spawn_requested",
            "tl.spawn_request_accepted",
        }:
            if payload.get("harness") != "codex/gpt-luna":
                raise AssertionError(
                    f"dispatch telemetry lost harness identity: {event}"
                )
            if payload.get("agent_type") != "codex":
                raise AssertionError(f"dispatch telemetry lost agent type: {event}")
            if payload.get("model") != "gpt-luna":
                raise AssertionError(f"dispatch telemetry lost model identity: {event}")
        elif event_type == "tl.dispatch_confirmed":
            for key, expected in (("agent_type", "codex"), ("model", "gpt-luna")):
                if key in payload and payload[key] != expected:
                    raise AssertionError(f"dispatch telemetry changed {key}: {event}")
        return {
            "success": True,
            "result": {
                "event_id": f"active-controller-event-{len(self.controller_events)}"
            },
        }

    def file_upward_pr(self) -> dict[str, object]:
        """File a real summary branch representing the TL's upward PR."""
        branch = "main.tl-loop-active"
        worktree = self.repo / ".exo" / "worktrees" / "tl-loop-active-upward"
        _git(self.repo, "worktree", "add", "-b", branch, str(worktree), "main")
        summary = worktree / "TL-UPWARD-PR.md"
        summary.write_text(
            "# Active TL wave\n\n"
            "Both disjoint slices were tested, reviewed, and merged by the "
            "programmatic TL controller.\n",
            encoding="utf-8",
        )
        _git(worktree, "add", "TL-UPWARD-PR.md")
        _git(worktree, "commit", "-m", "Record active TL wave result")
        _git(worktree, "push", "-u", "origin", branch)
        commit_sha = _git(worktree, "rev-parse", "HEAD")
        _git(self.repo, "worktree", "remove", "--force", str(worktree))
        _git(self.repo, "branch", "-D", branch)
        result = {
            "number": 2001,
            "branch": branch,
            "base_branch": "main",
            "commit_sha": commit_sha,
            "filed": True,
        }
        self.calls.append({"tool": "file_pr", "arguments": result})
        return result

    def _spawn_leaf(self, arguments: JsonObject) -> JsonObject:
        name = _string(arguments, "name")
        intent_id = _string(arguments, "intent_id")
        if name not in LEAVES:
            raise AssertionError(f"unexpected leaf {name!r}")
        if name in {pr.branch.rsplit(".", 1)[-1] for pr in self.prs.values()}:
            raise AssertionError(f"leaf {name!r} was spawned twice")
        branch = f"main.{name}"
        worktree = self.repo / ".exo" / "worktrees" / name
        _git(self.repo, "worktree", "add", "-b", branch, str(worktree), "main")
        source_path = worktree / "src" / f"{name}.txt"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(f"{name} contribution\n", encoding="utf-8")
        test_path = worktree / "tests" / f"test_{name.replace('-', '_')}.py"
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(
            "from pathlib import Path\n"
            "import unittest\n\n"
            "class SliceTest(unittest.TestCase):\n"
            f"    def test_disjoint_source_exists(self):\n        self.assertTrue(Path('src/{name}.txt').is_file())\n",
            encoding="utf-8",
        )
        _run(["python3", "-m", "unittest", "discover", "-s", "tests"], worktree)
        _git(worktree, "add", "src", "tests")
        _git(worktree, "commit", "-m", f"Implement {name}")
        _git(worktree, "push", "-u", "origin", branch)
        commit_sha = _git(worktree, "rev-parse", "HEAD")
        self._correlate_spawn_event(name, intent_id)
        pr_number = PR_NUMBERS[name]
        self.prs[pr_number] = PullRequest(
            number=pr_number,
            branch=branch,
            worktree=worktree,
            review_sha=REVIEW_SHAS[name],
            commit_sha=commit_sha,
        )
        return {"success": True, "result": {"branch": branch, "pr_number": pr_number}}

    def _correlate_spawn_event(self, name: str, intent_id: str) -> None:
        segment = self.ledger_segments / "segment-0001.jsonl"
        rows = [
            json.loads(line)
            for line in segment.read_text(encoding="utf-8").splitlines()
        ]
        for row in rows:
            if row.get("type") != "agent.spawned" or row.get("agent_id") != name:
                continue
            data = row.get("data")
            if not isinstance(data, dict):
                raise TypeError(f"spawn event data is not an object: {row}")
            data["intent_id"] = intent_id
            segment.write_text(
                "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows),
                encoding="utf-8",
            )
            return
        raise AssertionError(f"no authoritative spawn event exists for {name!r}")

    def _watcher_pr_state(self, arguments: JsonObject) -> JsonObject:
        pr_number = _positive_int(arguments, "pr_number")
        pr = self.prs.get(pr_number)
        if pr is None:
            raise AssertionError(f"review requested for unknown PR #{pr_number}")
        self.adjudications += 1
        return {
            "success": True,
            "result": {
                "found": True,
                "head_sha": pr.review_sha,
                "merge_ready": True,
                "review_state": "approved",
                "adjudication": "GO",
            },
        }

    def _merge_pr(self, arguments: JsonObject) -> JsonObject:
        pr_number = _positive_int(arguments, "pr_number")
        pr = self.prs.get(pr_number)
        if pr is None:
            raise AssertionError(f"merge requested for unknown PR #{pr_number}")
        if pr.merged:
            raise AssertionError(f"PR #{pr_number} was merged twice")
        _git(self.repo, "merge", "--no-edit", "--no-ff", pr.branch)
        _git(self.repo, "push", "origin", "main")
        _git(self.repo, "worktree", "remove", "--force", str(pr.worktree))
        _git(self.repo, "branch", "-D", pr.branch)
        _git(self.repo, "push", "origin", "--delete", pr.branch, check=False)
        pr.merged = True
        return {"success": True, "result": {"merged": True, "pr_number": pr_number}}


def run_active_wave(repo: Path, remote: Path, artifacts: Path) -> None:
    """Run, reconcile, and assert one real two-slice active trajectory."""
    _assert_no_tmux_session()
    segments = repo / ".exo" / "ledger" / "segments"
    state_root = repo / ".exo" / "tl-loop"
    _write_ledger(segments)
    transport = ActiveEffectTransport(repo, remote, segments)
    reader = LedgerReader(segments, run_id=RUN_ID, state_root=state_root)
    source = LazyLedgerQueue(reader, first_event_delay=0.05)
    plan = WorkPlan.from_mapping(
        {
            "leaves": [
                {
                    "name": "active-slice-a",
                    "task": "Add one disjoint fixture file and verify it",
                    "boundary": ["src/active-slice-a.txt"],
                    "verify": ["python3 -m unittest discover -s tests"],
                },
                {
                    "name": "active-slice-b",
                    "task": "Add one disjoint fixture file and verify it",
                    "boundary": ["src/active-slice-b.txt"],
                    "verify": ["python3 -m unittest discover -s tests"],
                },
            ]
        }
    )
    _assert_disjoint_boundaries(plan)
    policy = validate_policy(
        {
            "roles": {
                role: {
                    "allow": ["codex/gpt-luna"],
                    "cost_rank": {"codex/gpt-luna": 1},
                    "token_budget": 2000,
                    "per_harness_budget": {"codex/gpt-luna": 2000},
                    "escalate_after_attempts": 1,
                }
                for role in ("tl", "worker", "reviewer")
            }
        }
    )
    config = TLLoopConfig(
        active=True,
        max_workers=0,
        max_leaves=2,
        max_events=16,
        poll_interval=0.01,
        idle_timeout=0.01,
        dispatch_timeout=0.01,
        policy=policy,
        capabilities=CapabilityMap({"codex/gpt-luna": Difficulty.STANDARD}),
        source=source,
        effects=EffectClient(transport),
        root_dir=state_root,
        run_id=RUN_ID,
        role="worker",
    )
    try:
        try:
            result = tl_run(
                {"run_id": RUN_ID, "plan": plan},
                config,
                {"tokens": 0, "wall_seconds": 0},
            )
        except MutationBlocked as error:
            raise AssertionError("active run attempted a read-only mutation") from error
    finally:
        source.close()

    _reconcile_budgets(state_root, transport)
    _assert_result(result, transport, reader, state_root, repo)
    upward_pr = transport.file_upward_pr()
    _assert_upward_pr(repo, upward_pr)
    state = RunStore(RUN_ID, state_root).load()
    artifact = {
        "run_id": RUN_ID,
        "final_phase": state.fsm.phase.value,
        "merged_prs": sorted(pr.number for pr in transport.prs.values() if pr.merged),
        "effect_order": [cast(str, call["tool"]) for call in transport.calls],
        "adjudications": transport.adjudications,
        "ledger": {
            "sequence_status": reader.read_from(0).sequence_status.value,
            "event_count": len(reader.read_from(0).events),
            "last_consumed_offset": state.events.last_consumed_offset,
            "charges_reconciled": all(
                charge.reconciled for charge in state.budgets.charges
            ),
            "reserved_tokens": dict(state.budgets.role_reserved),
            "spent_tokens": dict(state.budgets.role_spent),
        },
        "upward_pr": upward_pr,
        "mutation_blocked": 0,
        "manual_interventions": [],
        "tmux_session_started": False,
        "worktrees_after_merge": _worktrees(repo),
    }
    artifacts.parent.mkdir(parents=True, exist_ok=True)
    artifacts.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(artifact, indent=2, sort_keys=True))


def _reconcile_budgets(state_root: Path, transport: ActiveEffectTransport) -> None:
    store = RunStore(RUN_ID, state_root)
    state = store.load()
    for name in LEAVES:
        updated_ledger = reconcile(state.budgets, name, ACTUAL_TOKENS[name])
        state = store.checkpoint(
            state.fsm,
            state.slices,
            {"ledger": updated_ledger},
            state.events.last_consumed_offset,
        )
    if len(transport.prs) != len(LEAVES):
        raise AssertionError("cannot reconcile a wave with missing PR records")


def _assert_result(
    result: TLRunResult,
    transport: ActiveEffectTransport,
    reader: LedgerReader,
    state_root: Path,
    repo: Path,
) -> None:
    state = RunStore(RUN_ID, state_root).load()
    if (
        result.final_state.fsm.phase is not TLPhase.TLDone
        or state.fsm.phase is not TLPhase.TLDone
    ):
        raise AssertionError(f"active loop did not finish at TLDone: {state.fsm.phase}")
    if state.gates:
        raise AssertionError(
            f"observational delay opened lifecycle gates: {state.gates}"
        )
    if state.goals.controller_started_at is None:
        raise AssertionError("controller start telemetry was not persisted")
    if state.goals.last_authoritative_event_seq != 9:
        raise AssertionError(
            "last authoritative event telemetry did not reach the final ledger sequence"
        )
    if time.time() - state.goals.controller_started_at < 0.04:
        raise AssertionError("active E2E did not cross its former lifecycle deadline")
    effect_tools = [
        cast(str, call["tool"])
        for call in transport.calls
        if call["tool"] != "emit_controller_event"
    ]
    if effect_tools != [
        "spawn_leaf",
        "spawn_leaf",
        "watcher_pr_state",
        "merge_pr",
        "watcher_pr_state",
        "merge_pr",
    ]:
        raise AssertionError(f"unexpected active effect order: {transport.calls}")
    dispatch_events = {
        event["event_type"]
        for event in transport.controller_events
        if cast(str, event["event_type"]).startswith(("tl.dispatch_", "tl.spawn_"))
    }
    if not {
        "tl.dispatch_intended",
        "tl.spawn_requested",
        "tl.spawn_request_accepted",
        "tl.dispatch_confirmed",
    }.issubset(dispatch_events):
        raise AssertionError(
            f"dispatch telemetry was incomplete: {transport.controller_events}"
        )
    if transport.adjudications != 2 or len(transport.prs) != 2:
        raise AssertionError("both PRs must receive one deterministic approval")
    if not all(pr.merged for pr in transport.prs.values()):
        raise AssertionError("both child PRs must be merged")
    for name in LEAVES:
        source_path = repo / "src" / f"{name}.txt"
        if not source_path.is_file():
            raise AssertionError(f"merged source file missing: {source_path}")
    read_result = reader.read_from(0)
    if read_result.sequence_status is not SequenceStatus.COMPLETE:
        raise AssertionError(
            f"ledger sequence is not gap-free: {read_result.sequence_status}"
        )
    if read_result.findings:
        raise AssertionError(f"ledger reader findings: {read_result.findings}")
    if [event.run_seq for event in read_result.events] != list(range(1, 10)):
        raise AssertionError("active ledger did not contain the expected nine events")
    if state.events.last_consumed_offset != 9:
        raise AssertionError(
            f"unexpected consumed ledger offset: {state.events.last_consumed_offset}"
        )
    if len(state.budgets.charges) != 2 or not all(
        charge.reconciled for charge in state.budgets.charges
    ):
        raise AssertionError(
            f"budget charges were not reconciled: {state.budgets.charges}"
        )
    if state.budgets.role_reserved or state.budgets.harness_reserved:
        raise AssertionError("budget reservations remain after reconciliation")
    if state.budgets.role_spent != {"worker": 1200}:
        raise AssertionError(f"unexpected role spend: {state.budgets.role_spent}")
    worktrees = _worktrees(repo)
    if worktrees != [str(repo)]:
        raise AssertionError(f"child worktrees were not cleaned: {worktrees}")
    if _current_branch(repo) != "main":
        raise AssertionError("scratch repository did not return to main")


def _assert_upward_pr(repo: Path, upward_pr: dict[str, object]) -> None:
    if upward_pr.get("filed") is not True or upward_pr.get("base_branch") != "main":
        raise AssertionError(f"upward PR was not filed against main: {upward_pr}")
    branch = cast(str, upward_pr["branch"])
    _git(repo, "ls-remote", "--exit-code", "--heads", "origin", branch)


def _write_ledger(segments: Path) -> None:
    segments.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for seq, name in enumerate(LEAVES, start=1):
        rows.append(
            _event(
                seq,
                "agent.spawned",
                name,
                {
                    "child_agent": name,
                    "agent_type": "codex/gpt-luna",
                    "branch": f"main.{name}",
                },
            )
        )
    rows.extend(
        [
            _event(
                3,
                "pr.review",
                "active-slice-a",
                {
                    "slice_id": "active-slice-a",
                    "kind": "approved",
                    "review_state": "approved",
                    "head_sha": REVIEW_SHAS["active-slice-a"],
                    "pr_number": PR_NUMBERS["active-slice-a"],
                },
            ),
            _event(
                4,
                "pr.review",
                "active-slice-b",
                {
                    "slice_id": "active-slice-b",
                    "kind": "approved",
                    "review_state": "approved",
                    "head_sha": REVIEW_SHAS["active-slice-b"],
                    "pr_number": PR_NUMBERS["active-slice-b"],
                },
            ),
            _event(
                5,
                "ci.status_changed",
                "active-slice-a",
                {
                    "slice_id": "active-slice-a",
                    "head_sha": REVIEW_SHAS["active-slice-a"],
                    "pr_number": PR_NUMBERS["active-slice-a"],
                    "status": "success",
                },
            ),
            _event(
                6,
                "ci.status_changed",
                "active-slice-b",
                {
                    "slice_id": "active-slice-b",
                    "head_sha": REVIEW_SHAS["active-slice-b"],
                    "pr_number": PR_NUMBERS["active-slice-b"],
                    "status": "success",
                },
            ),
            _event(
                7,
                "agent.completed",
                "active-slice-a",
                {"status": "success", "pr_number": PR_NUMBERS["active-slice-a"]},
            ),
            _event(
                8,
                "agent.completed",
                "active-slice-b",
                {"status": "success", "pr_number": PR_NUMBERS["active-slice-b"]},
            ),
            _event(
                9,
                "agent.notify_parent",
                None,
                {"status": "success", "shadow_event": {"kind": "all_children_done"}},
            ),
        ]
    )
    if [cast(int, row["run_seq"]) for row in rows] != list(range(1, 10)):
        raise AssertionError("active ledger fixture has a sequence gap")
    segment = segments / "segment-0001.jsonl"
    segment.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _event(
    sequence: int,
    event_type: str,
    agent_id: str | None,
    data: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": f"active-event-{sequence}",
        "id": f"active-event-{sequence}",
        "event_time": ACTIVE_EVENT_TIME,
        "observed_at": ACTIVE_EVENT_TIME,
        "run_seq": sequence,
        "type": event_type,
        "agent_id": agent_id,
        "run_id": RUN_ID,
        "session_id": "active-controller",
        "lifecycle_state": "observed",
        "data": data,
    }


@dataclass
class LazyLedgerQueue:
    """Start the ledger tailer after ``run_tl_loop`` publishes its checkpoint."""

    reader: LedgerReader
    first_event_delay: float = 0.0
    queue: LedgerQueue | None = None
    delayed: bool = False

    def get(self, timeout: float | None = None) -> Any:
        if self.queue is None:
            self.queue = LedgerQueue(self.reader, poll_interval=0.01).start()
        if not self.delayed and self.first_event_delay:
            time.sleep(self.first_event_delay)
            self.delayed = True
        return self.queue.get(timeout)

    def acknowledge(self, event: Any) -> int:
        if self.queue is None:
            raise AssertionError("ledger queue was not started")
        return self.queue.acknowledge(event)

    def close(self) -> None:
        if self.queue is not None:
            self.queue.close(timeout=2.0)


def _assert_disjoint_boundaries(plan: WorkPlan) -> None:
    paths = [path for leaf in plan.leaves for path in leaf.boundary]
    if len(paths) != len(set(paths)):
        raise AssertionError(f"slice boundaries overlap: {paths}")


def _worktrees(repo: Path) -> list[str]:
    output = _git(repo, "worktree", "list", "--porcelain")
    return [
        line.removeprefix("worktree ")
        for line in output.splitlines()
        if line.startswith("worktree ")
    ]


def _current_branch(repo: Path) -> str:
    return _git(repo, "branch", "--show-current")


def _assert_no_tmux_session() -> None:
    if shutil.which("tmux") is None:
        raise AssertionError("tmux is required to assert that no session was started")
    result = subprocess.run(
        ["tmux", "has-session", "-t", SESSION],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode == 0:
        raise AssertionError(f"refusing to reuse existing tmux session {SESSION!r}")


def _git(cwd: Path, *arguments: str, check: bool = True) -> str:
    return _run(["git", *arguments], cwd, check=check).stdout.strip()


def _run(
    command: list[str], cwd: Path, check: bool = True
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command, cwd=cwd, check=False, text=True, capture_output=True
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed


def _string(arguments: JsonObject, key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise AssertionError(f"effect argument {key!r} must be non-empty text")
    return value


def _positive_int(arguments: JsonObject, key: str) -> int:
    value = arguments.get(key)
    if type(value) is not int or value <= 0:
        raise AssertionError(f"effect argument {key!r} must be positive")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--remote", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    arguments = parser.parse_args()
    run_active_wave(
        arguments.repo.resolve(),
        arguments.remote.resolve(),
        arguments.artifacts.resolve(),
    )


if __name__ == "__main__":
    main()
