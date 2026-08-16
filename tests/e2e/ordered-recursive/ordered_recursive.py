#!/usr/bin/env python3
"""Exercise recursive ordered integration through the production TL controller.

The fixture uses real Git worktrees, real tmux windows, and the Forgejo REST
surface (or the repository's local Forgejo-shaped mock).  All dispatch,
recursive waiting, review/CI routing, evidence capture, and ordered merging
are performed by the function run_tl_loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import JsonObject
from tl_loop.events.envelope import EventEnvelope, project
from tl_loop.loop.driver import (
    IntegrationContract,
    LeafTask,
    SubTLTask,
    TLLoopConfig,
    WorkPlan,
    run_tl_loop,
)
from tl_loop.rlm.store import RlmCallStore, RlmModelChoice, RlmResponse
from tl_loop.state.schema import Verdict


class HarnessError(RuntimeError):
    """The ordered integration acceptance contract was violated."""


def run(command: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise HarnessError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout.strip()


def git(repo: Path, *arguments: str) -> str:
    return run(["git", "-C", str(repo), *arguments])


def tmux(*arguments: str) -> str:
    return run(["tmux", *arguments])


def json_request(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/json")
    request.add_header("Authorization", f"token {token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise HarnessError(
            f"Forgejo {method} {url} failed: HTTP {error.code}: {detail}"
        ) from error
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise HarnessError(f"Forgejo returned non-JSON for {method} {url}") from error


@dataclass
class ForgejoApi:
    base_url: str
    token: str
    owner: str
    repo: str
    mock: bool

    @property
    def api_root(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/v1/repos/{self.owner}/{self.repo}"

    def create_pr(self, name: str, branch: str, base: str) -> dict[str, Any]:
        result = json_request(
            "POST",
            f"{self.api_root}/pulls",
            self.token,
            {
                "title": f"Ordered recursive stage {name}",
                "body": f"Owner: ordered-e2e:{name}\nHead: {branch}\nBase: {base}",
                "head": branch,
                "base": base,
            },
        )
        if not isinstance(result, dict) or not isinstance(result.get("number"), int):
            raise HarnessError(
                f"Forgejo create PR returned an invalid response: {result!r}"
            )
        return result

    def get_pr(self, number: int) -> dict[str, Any]:
        result = json_request("GET", f"{self.api_root}/pulls/{number}", self.token)
        if not isinstance(result, dict):
            raise HarnessError(f"Forgejo PR response is not an object: {result!r}")
        return result

    def list_open_prs(self) -> list[dict[str, Any]]:
        result = json_request("GET", f"{self.api_root}/pulls?state=open", self.token)
        if not isinstance(result, list) or not all(
            isinstance(pr, dict) for pr in result
        ):
            raise HarnessError(f"Forgejo PR list is not an object array: {result!r}")
        return result

    def merge(self, number: int) -> None:
        json_request(
            "POST",
            f"{self.api_root}/pulls/{number}/merge",
            self.token,
            {"Do": "merge"},
        )


@dataclass
class EventStream:
    """Thread-safe projected event source for one controller owner."""

    events: queue.Queue[EventEnvelope] = field(default_factory=queue.Queue)
    acknowledged: list[int] = field(default_factory=list)

    def get(self, timeout: float | None = None) -> EventEnvelope:
        return self.events.get(timeout=timeout)

    def put(self, event: EventEnvelope) -> None:
        self.events.put(event)

    def acknowledge(self, event: EventEnvelope) -> int:
        if event.run_seq is None:
            raise HarnessError("controller acknowledged an event without run_seq")
        self.acknowledged.append(event.run_seq)
        return event.run_seq


@dataclass
class LeafRecord:
    owner: str
    name: str
    branch: str
    base: str
    worktree: Path
    started: Path
    finished: Path
    pr_number: int | None = None
    head_sha: str | None = None
    merged: bool = False


@dataclass
class AggregateRecord:
    owner: str
    branch: str
    base: str
    worktree: Path
    pr_number: int
    head_sha: str
    patch_digest: str
    merged: bool = False


def wait_for(path: Path, *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise HarnessError(f"timed out waiting for {path}")
        time.sleep(0.02)


def start_leaf(
    repo: Path,
    work_root: Path,
    session: str,
    name: str,
    base: str,
    relative_file: str,
    *,
    branch: str,
) -> tuple[Path, Path, Path]:
    worktree = work_root / "worktrees" / "leaf" / name.replace("/", "_")
    marker_dir = work_root / "markers"
    marker_dir.mkdir(parents=True, exist_ok=True)
    started = marker_dir / f"{name.replace('/', '_')}.started"
    finished = marker_dir / f"{name.replace('/', '_')}.finished"
    worker = work_root / "workers" / f"{name.replace('/', '_')}.sh"
    worker.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(worktree), base)
    worker.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        f"date +%s%N > '{started}'\n"
        "sleep 0.20\n"
        f"mkdir -p \"$(dirname '{worktree / relative_file}')\"\n"
        f"printf '%s\\n' '{name} contribution' > '{worktree / relative_file}'\n"
        f"git -C '{worktree}' add '{relative_file}'\n"
        f"git -C '{worktree}' -c user.name='ordered-e2e' -c user.email='ordered-e2e@example.com' commit -m 'Implement {name}'\n"
        f"git -C '{worktree}' push -u origin '{branch}'\n"
        f"date +%s%N > '{finished}'\n",
        encoding="utf-8",
    )
    worker.chmod(0o700)
    tmux("new-window", "-d", "-t", session, "-n", name, str(worker))
    return worktree, started, finished


class ControllerScenario:
    """Forgejo-shaped effect transport used by the real TL controller."""

    def __init__(
        self,
        repo: Path,
        work_root: Path,
        api: ForgejoApi,
        session: str,
        root_source: EventStream,
    ) -> None:
        self.repo = repo
        self.work_root = work_root
        self.api = api
        self.session = session
        self.root_source = root_source
        self.streams: dict[str, EventStream] = {}
        self.parent_streams: dict[str, EventStream] = {}
        self.owner_worktrees: dict[str, Path] = {}
        self.owner_branches: dict[str, str] = {}
        self.leaves: dict[int, LeafRecord] = {}
        self.aggregates: dict[int, AggregateRecord] = {}
        self.top_ready_order: list[str] = []
        self.leaf_threads: list[threading.Thread] = []
        self.controller_events: list[tuple[str, JsonObject]] = []
        self.failures: list[tuple[str, str, str]] = []
        self._next_seq: dict[str, int] = {}
        self._completed: dict[str, int] = {}
        self._expected_leaves: dict[str, int] = {}
        self._held_alpha: tuple[AggregateRecord, EventStream] | None = None
        self._held_beta: tuple[AggregateRecord, EventStream] | None = None
        self._lock = threading.Lock()
        self._api_lock = threading.Lock()

    def prepare_owner(
        self,
        owner: str,
        source: EventStream,
        parent_source: EventStream,
        *,
        expected_leaves: int,
    ) -> None:
        branch = f"main.{owner}"
        base = branch.rpartition(".")[0] or "main"
        worktree = self.work_root / "worktrees" / "aggregate" / owner.replace("/", "_")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if not self._local_branch_exists(branch):
            git(self.repo, "worktree", "add", "-b", branch, str(worktree), base)
        self.streams[owner] = source
        self.parent_streams[owner] = parent_source
        self.owner_worktrees[owner] = worktree
        self.owner_branches[owner] = branch
        self._expected_leaves[owner] = expected_leaves
        self._completed[owner] = 0

    def _local_branch_exists(self, branch: str) -> bool:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "show-ref",
                "--verify",
                f"refs/heads/{branch}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def _event(
        self,
        owner: str,
        event_type: str,
        agent_id: str | None,
        data: JsonObject,
        *,
        run_id: str | None = None,
    ) -> EventEnvelope:
        with self._lock:
            seq = self._next_seq.get(owner, 0) + 1
            self._next_seq[owner] = seq
        observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        raw: dict[str, object] = {
            "schema_version": 1,
            "event_id": f"{owner}-event-{seq}",
            "id": f"{owner}-event-{seq}",
            "event_time": observed_at,
            "observed_at": observed_at,
            "run_seq": seq,
            "type": event_type,
            "agent_id": agent_id,
            "run_id": run_id or (owner if owner in self.streams else "ordered-root"),
            "session_id": self.session,
            "lifecycle_state": "observed",
            "data": data,
        }
        return project(raw)

    def _put(
        self, owner: str, event_type: str, agent_id: str | None, data: JsonObject
    ) -> None:
        stream = self.streams.get(owner)
        if stream is None:
            raise HarnessError(f"no event stream registered for {owner}")
        stream.put(self._event(owner, event_type, agent_id, data))

    def _relative_file(self, owner: str, leaf_name: str) -> str:
        suffix = leaf_name.rsplit("leaf-", 1)[-1]
        if owner.endswith("nested-one"):
            return f"nested/one-{suffix}.txt"
        if owner.endswith("nested-two"):
            return f"nested/two-{suffix}.txt"
        return f"src/{leaf_name}.txt"

    def _finish_leaf(self, record: LeafRecord) -> None:
        try:
            # Let the controller finish dispatching the sibling batch before
            # the first fast tmux worker can report completion.
            time.sleep(0.25)
            wait_for(record.finished)
            head_sha = git(record.worktree, "rev-parse", "HEAD")
            with self._api_lock:
                pr = self.api.create_pr(record.name, record.branch, record.base)
            if pr.get("head", {}).get("sha") != head_sha:
                raise HarnessError(
                    f"Forgejo head differs from local leaf {record.name}"
                )
            record.pr_number = int(pr["number"])
            record.head_sha = head_sha
            self.leaves[record.pr_number] = record
            self._put(
                record.owner,
                "agent.notify_parent",
                record.name,
                {
                    "status": "success",
                    "message": "completed",
                    "child_agent": record.name,
                    "pr_number": record.pr_number,
                    "head_sha": head_sha,
                },
            )
            with self._lock:
                self._completed[record.owner] += 1
                complete = (
                    self._completed[record.owner] == self._expected_leaves[record.owner]
                )
            if complete:
                self._put(
                    record.owner,
                    "agent.notify_parent",
                    None,
                    {"shadow_event": {"kind": "all_children_done"}},
                )
        except Exception as error:  # pragma: no cover - surfaced by the controller
            self._put(
                record.owner,
                "agent.stuck",
                record.name,
                {"reason": str(error)},
            )

    def _publish_aggregate_events(
        self, record: AggregateRecord, target: EventStream
    ) -> None:
        if record.owner in {"alpha", "beta"}:
            self.top_ready_order.append(record.owner)
        head = record.head_sha
        review = {
            "slice_id": record.owner,
            "pr_number": record.pr_number,
            "head_sha": head,
            "patch_digest": record.patch_digest,
            "kind": "review",
            "findings": [],
            "diff": {
                "diff": "@@ -1 +1 @@\n-old\n+new\n",
                "lines_changed": 1,
                "paths": ["src/ordered-recursive.txt"],
                "review_rounds": 1,
            },
        }
        ci = {
            "slice_id": record.owner,
            "pr_number": record.pr_number,
            "head_sha": head,
            "status": "success",
        }
        parent_run_id = record.owner.rpartition(".")[0] or "ordered-root"
        review_event = self._event(
            record.owner,
            "pr.review",
            record.owner,
            review,
            run_id=parent_run_id,
        )
        ci_event = self._event(
            record.owner,
            "ci.status_changed",
            record.owner,
            ci,
            run_id=parent_run_id,
        )
        target.put(review_event)
        target.put(ci_event)

    def _publish_or_hold(self, record: AggregateRecord, target: EventStream) -> None:
        ready: list[tuple[AggregateRecord, EventStream]] = []
        with self._lock:
            if record.owner == "alpha":
                if self._held_beta is None:
                    self._held_alpha = (record, target)
                    return
                held, held_target = self._held_beta
                self._held_beta = None
                ready = [(held, held_target), (record, target)]
            elif record.owner == "beta":
                if self._held_alpha is None:
                    self._held_beta = (record, target)
                    return
                held, held_target = self._held_alpha
                self._held_alpha = None
                ready = [(record, target), (held, held_target)]
            else:
                ready = [(record, target)]
        for item, item_target in ready:
            self._publish_aggregate_events(item, item_target)

    def _patch_digest(self, base: str, head: str) -> str:
        patch = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "diff",
                "--binary",
                "--no-ext-diff",
                f"{base}...{head}",
            ],
            check=True,
            capture_output=True,
        ).stdout
        return hashlib.sha256(patch).hexdigest()

    def _merge_tree(self, base: str, head: str) -> str:
        return git(self.repo, "merge-tree", "--write-tree", base, head).split()[0]

    def _snapshot(self, record: LeafRecord | AggregateRecord) -> JsonObject:
        base_sha = git(self.repo, "rev-parse", record.base)
        head_sha = git(self.repo, "rev-parse", record.branch)
        return {
            "open": not record.merged,
            "merged": record.merged,
            "head_branch": record.branch,
            "head_sha": head_sha,
            "base_sha": base_sha,
            "patch_digest": self._patch_digest(base_sha, head_sha),
            "merge_tree_sha": self._merge_tree(base_sha, head_sha),
            "ci_status": "success",
        }

    def _merge_local(self, record: LeafRecord | AggregateRecord) -> None:
        if isinstance(record, LeafRecord):
            worktree = self.owner_worktrees[record.owner]
            git(worktree, "merge", "--no-edit", "--no-ff", record.branch)
            git(worktree, "push", "origin", record.base)
        else:
            owner = record.base.removeprefix("main.")
            worktree = (
                self.repo if record.base == "main" else self.owner_worktrees[owner]
            )
            git(worktree, "merge", "--no-edit", "--no-ff", record.branch)
            git(worktree, "push", "origin", record.base)
        with self._api_lock:
            self.api.merge(record.pr_number)
        record.merged = True

    def _call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        del role
        if tool_name == "emit_controller_event":
            event_type = str(arguments.get("event_type", ""))
            payload = arguments.get("payload", {})
            if isinstance(payload, dict):
                self.controller_events.append((event_type, payload))
            return {
                "success": True,
                "result": {"event_id": "ordered-e2e-event", "run_seq": 1},
            }
        if tool_name == "spawn_leaf":
            owner = name
            leaf_name = str(arguments["name"])
            branch = f"{self.owner_branches[owner]}.{leaf_name}"
            worktree, started, finished = start_leaf(
                self.repo,
                self.work_root,
                self.session,
                leaf_name,
                self.owner_branches[owner],
                self._relative_file(owner, leaf_name),
                branch=branch,
            )
            record = LeafRecord(
                owner,
                leaf_name,
                branch,
                self.owner_branches[owner],
                worktree,
                started,
                finished,
            )
            self._put(
                owner,
                "agent.spawned",
                leaf_name,
                {
                    "child_agent": leaf_name,
                    "agent_type": "codex",
                    "branch": branch,
                    "intent_id": str(arguments["intent_id"]),
                },
            )
            thread = threading.Thread(
                target=self._finish_leaf, args=(record,), daemon=True
            )
            self.leaf_threads.append(thread)
            thread.start()
            return {"success": True, "result": {"agent_id": leaf_name}}
        if tool_name == "file_pr":
            owner = name
            branch = self.owner_branches[owner]
            base = str(arguments.get("base_branch") or "main")
            base_sha = git(self.repo, "rev-parse", base)
            head_sha = git(self.repo, "rev-parse", branch)
            record = AggregateRecord(
                owner,
                branch,
                base,
                self.owner_worktrees[owner],
                0,
                head_sha,
                self._patch_digest(base_sha, head_sha),
            )
            with self._api_lock:
                pr = self.api.create_pr(owner, branch, base)
            record.pr_number = int(pr["number"])
            if pr.get("head", {}).get("sha") != head_sha:
                raise HarnessError(f"Forgejo head differs from local aggregate {owner}")
            self.aggregates[record.pr_number] = record
            self._publish_or_hold(record, self.parent_streams[owner])
            return {
                "success": True,
                "result": {
                    "pr_number": record.pr_number,
                    "head_sha": record.head_sha,
                    "patch_digest": record.patch_digest,
                    "base_sha": base_sha,
                },
            }
        if tool_name == "watcher_pr_state":
            number = int(arguments["pr_number"])
            record = self.leaves.get(number) or self.aggregates.get(number)
            if record is None:
                raise HarnessError(f"watcher requested unknown PR #{number}")
            return {"success": True, "result": self._snapshot(record)}
        if tool_name == "merge_pr":
            number = int(arguments["pr_number"])
            record = self.leaves.get(number) or self.aggregates.get(number)
            if record is None:
                raise HarnessError(f"merge requested unknown PR #{number}")
            expected = {
                "base_sha": arguments.get("expected_base_sha"),
                "head_sha": arguments.get("expected_head_sha"),
                "patch_digest": arguments.get("expected_patch_digest"),
                "merge_tree_sha": arguments.get("expected_merge_tree_sha"),
            }
            if any(value is not None for value in expected.values()):
                actual = self._snapshot(record)
                for key, value in expected.items():
                    if value is not None and value != actual[key]:
                        return {
                            "success": False,
                            "error": f"compare_and_swap: expected {key} {value}, found {actual[key]}",
                        }
            self._merge_local(record)
            return {"success": True, "result": {"merged": True}}
        if tool_name == "spawn_reviewer":
            return {"success": True, "result": {"agent_id": "ordered-reviewer"}}
        return {"success": True, "result": None}

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        try:
            return self._call_tool(role, name, tool_name, arguments)
        except Exception as error:
            self.failures.append((name, tool_name, str(error)))
            raise

    def cleanup(self) -> None:
        for thread in self.leaf_threads:
            thread.join(timeout=5)
        paths = [record.worktree for record in self.leaves.values()]
        paths.extend(self.owner_worktrees.values())
        for path in sorted(set(paths), key=lambda value: len(str(value)), reverse=True):
            if path.exists():
                git(self.repo, "worktree", "remove", "--force", str(path))
        git(self.repo, "worktree", "prune")


class ReviewBackend:
    """Deterministic model boundary for the controller's review adjudication."""

    def complete(self, request: Any) -> RlmResponse:
        sections = request.inputs["sections"]
        head = next(
            section["content"]
            for section in sections
            if section["name"] == "reviewed_head"
        )
        return RlmResponse(
            {
                "verdict": Verdict.GO.value,
                "reviewed_head": head,
                "reasons": [],
                "blocking_count": 0,
            }
        )


def _review_choice() -> RlmModelChoice:
    return RlmModelChoice(
        model_id="ordered-e2e-model",
        backend=ReviewBackend(),
        store=RlmCallStore(),
        context_length=20_000,
    )


def _leaf_plan(owner: str) -> WorkPlan:
    leaves = tuple(
        LeafTask(
            name=f"{owner}.leaf-{suffix}",
            task=f"produce the {owner} leaf {suffix} contribution",
            boundary=(f"src/{owner}.leaf-{suffix}.txt",),
            verify=("git diff --check",),
        )
        for suffix in ("a", "b")
    )
    return WorkPlan(leaves=leaves)


def _build_plan(scenario: ControllerScenario, root_source: EventStream) -> WorkPlan:
    alpha_source = EventStream()
    beta_source = EventStream()
    nested_one_source = EventStream()
    nested_two_source = EventStream()
    scenario.prepare_owner("alpha", alpha_source, root_source, expected_leaves=0)
    scenario.prepare_owner("beta", beta_source, root_source, expected_leaves=2)
    scenario.prepare_owner(
        "alpha.nested-one", nested_one_source, alpha_source, expected_leaves=2
    )
    scenario.prepare_owner(
        "alpha.nested-two", nested_two_source, alpha_source, expected_leaves=2
    )
    nested = tuple(
        SubTLTask(
            name=name,
            plan=_leaf_plan(name),
            source=source,
            worktree=scenario.owner_worktrees[name],
            order=1,
        )
        for name, source in (
            ("alpha.nested-one", nested_one_source),
            ("alpha.nested-two", nested_two_source),
        )
    )
    return WorkPlan(
        sub_tls=(
            SubTLTask(
                name="alpha",
                plan=WorkPlan(sub_tls=nested),
                source=alpha_source,
                worktree=scenario.owner_worktrees["alpha"],
                order=1,
                integration=IntegrationContract(),
            ),
            SubTLTask(
                name="beta",
                plan=_leaf_plan("beta"),
                source=beta_source,
                worktree=scenario.owner_worktrees["beta"],
                order=1,
                integration=IntegrationContract(),
            ),
        )
    )


def _assert_result(scenario: ControllerScenario, result: Any, repo: Path) -> None:
    if getattr(result.final_state.fsm.phase, "value", None) != "tl_done":
        raise HarnessError(
            "controller did not finish: "
            f"{result.final_state.fsm.phase!r}; "
            f"slices={[(name, state.status.value) for name, state in result.final_state.slices.items()]}; "
            f"failures={scenario.failures}"
        )
    for name in ("alpha", "beta"):
        if result.final_state.slices[name].status.value != "merged":
            raise HarnessError(f"aggregate {name} did not merge")
    if scenario.top_ready_order != ["beta", "alpha"]:
        raise HarnessError(
            f"review readiness was not intentionally out of order: {scenario.top_ready_order}"
        )
    if not any(
        event == "tl.integration_revalidated" for event, _ in scenario.controller_events
    ):
        raise HarnessError(
            "controller did not revalidate integration evidence before merging"
        )
    if not any(
        event == "tl.stage_completed" for event, _ in scenario.controller_events
    ):
        raise HarnessError("controller did not report ordered stage completion")
    if scenario.leaf_threads and not all(
        not thread.is_alive() for thread in scenario.leaf_threads
    ):
        raise HarnessError(
            "a real tmux leaf remained alive after controller completion"
        )
    if git(repo, "show", "main:src/beta.leaf-b.txt") != "beta.leaf-b contribution":
        raise HarnessError("beta leaf was not integrated")
    if (
        git(repo, "show", "main:nested/one-a.txt")
        != "alpha.nested-one.leaf-a contribution"
    ):
        raise HarnessError("nested ordered stage was not integrated")
    if (
        git(repo, "show", "main:nested/two-a.txt")
        != "alpha.nested-two.leaf-a contribution"
    ):
        raise HarnessError("second nested stage was not integrated")
    if scenario.api.list_open_prs():
        raise HarnessError("controller left ordered PRs open")
    if len(scenario.aggregates) != 4:
        raise HarnessError(
            f"expected four aggregate PRs, got {len(scenario.aggregates)}"
        )


def main() -> None:
    repo = Path(os.environ["ORDERED_E2E_REPO"]).resolve()
    work_root = Path(os.environ["ORDERED_E2E_WORK"]).resolve()
    api = ForgejoApi(
        os.environ["ORDERED_E2E_FORGEJO_URL"],
        os.environ.get("ORDERED_E2E_FORGEJO_TOKEN", ""),
        os.environ["ORDERED_E2E_FORGEJO_OWNER"],
        os.environ["ORDERED_E2E_FORGEJO_REPO"],
        os.environ.get("ORDERED_E2E_FORGEJO_MOCK") == "1",
    )
    session = f"ordered-e2e-{os.getpid()}"
    root_source = EventStream()
    scenario = ControllerScenario(repo, work_root, api, session, root_source)
    tmux("new-session", "-d", "-s", session, "sleep", "300")
    try:
        plan = _build_plan(scenario, root_source)
        effects = EffectClient(scenario)
        config = TLLoopConfig(
            active=True,
            max_events=128,
            max_parallel_slices=2,
            poll_interval=0.01,
            idle_timeout=30.0,
            dispatch_timeout=5.0,
            keep_alive_on_waiting=True,
            review_model_choice=_review_choice(),
            review_policy_path=Path(__file__).resolve().parents[3]
            / ".exo"
            / "review-policy.toml",
            root_dir=work_root / "state",
            run_id="ordered-root",
            branch="main",
            worktree=repo,
            source=root_source,
            effects=effects,
        )
        result = run_tl_loop(
            "ordered-root",
            plan,
            root_source,
            effects,
            config=config,
            root_dir=work_root / "state",
        )
        _assert_result(scenario, result, repo)
        print(
            json.dumps(
                {
                    "passed": True,
                    "controller": "run_tl_loop",
                    "same_order": ["alpha", "beta"],
                    "ready_order": scenario.top_ready_order,
                    "aggregate_prs": sorted(scenario.aggregates),
                    "recursive_owners": ["alpha.nested-one", "alpha.nested-two"],
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        scenario.cleanup()
        tmux("kill-session", "-t", session)


if __name__ == "__main__":
    try:
        main()
    except (HarnessError, KeyError) as error:
        raise SystemExit(f"ordered recursive Forgejo E2E failed: {error}") from error
