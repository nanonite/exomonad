#!/usr/bin/env python3
"""Exercise recursive ordered integration against Git and Forgejo.

The harness deliberately keeps the controller boundary visible: each leaf is
run in its own tmux window, aggregate branches are built from real worktrees,
and review/CI/merge observations come from the Forgejo REST API.  The
existing ``mock_github.py`` can provide a local API fixture, but the same
script runs against a dedicated real Forgejo repository.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
        raise HarnessError(f"Forgejo {method} {url} failed: HTTP {error.code}: {detail}") from error
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
                "title": f"Aggregate {name} recursive ordered stage",
                "body": f"Owner: ordered-e2e:{name}\nHead: {branch}\nBase: {base}",
                "head": branch,
                "base": base,
            },
        )
        if not isinstance(result, dict) or not isinstance(result.get("number"), int):
            raise HarnessError(f"Forgejo create PR returned an invalid response: {result!r}")
        return result

    def get_pr(self, number: int) -> dict[str, Any]:
        result = json_request("GET", f"{self.api_root}/pulls/{number}", self.token)
        if not isinstance(result, dict):
            raise HarnessError(f"Forgejo PR response is not an object: {result!r}")
        return result

    def list_open_prs(self) -> list[dict[str, Any]]:
        result = json_request("GET", f"{self.api_root}/pulls?state=open", self.token)
        if not isinstance(result, list) or not all(isinstance(pr, dict) for pr in result):
            raise HarnessError(f"Forgejo PR list is not an object array: {result!r}")
        return result

    def reviews(self, number: int) -> list[dict[str, Any]]:
        result = json_request("GET", f"{self.api_root}/pulls/{number}/reviews", self.token)
        if not isinstance(result, list) or not all(isinstance(review, dict) for review in result):
            raise HarnessError(f"Forgejo review list is not an object array: {result!r}")
        return result

    def approve(self, number: int, head_sha: str) -> None:
        if self.mock:
            json_request(
                "POST",
                f"{self.base_url.rstrip('/')}/_control/reviews",
                self.token,
                {"pr_number": number, "state": "APPROVED", "commit_id": head_sha},
            )
            return
        json_request(
            "POST",
            f"{self.api_root}/pulls/{number}/reviews",
            self.token,
            {"event": "APPROVE", "body": "Ordered integration acceptance", "commit_id": head_sha},
        )

    def status(self, head_sha: str) -> str:
        result = json_request("GET", f"{self.api_root}/commits/{head_sha}/status", self.token)
        if not isinstance(result, dict):
            raise HarnessError(f"Forgejo status response is not an object: {result!r}")
        state = result.get("state")
        if not isinstance(state, str):
            raise HarnessError(f"Forgejo status has no state: {result!r}")
        return state.lower()

    def merge(self, number: int) -> None:
        json_request(
            "POST",
            f"{self.api_root}/pulls/{number}/merge",
            self.token,
            {"Do": "merge"},
        )


def wait_for(path: Path, *, timeout: float = 10.0) -> None:
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
) -> tuple[str, Path, Path, Path]:
    branch = f"main.{name}"
    worktree = work_root / "worktrees" / name
    marker_dir = work_root / "markers"
    marker_dir.mkdir(parents=True, exist_ok=True)
    started = marker_dir / f"{name}.started"
    finished = marker_dir / f"{name}.finished"
    worker = work_root / "workers" / f"{name}.sh"
    worker.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(worktree), base)
    worker.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        f"date +%s%N > {started}\n"
        "sleep 0.20\n"
        f"mkdir -p \"$(dirname '{worktree / relative_file}')\"\n"
        f"printf '%s\\n' '{name} contribution' > '{worktree / relative_file}'\n"
        f"git -C '{worktree}' add '{relative_file}'\n"
        f"git -C '{worktree}' -c user.name='ordered-e2e' -c user.email='ordered-e2e@example.com' commit -m 'Implement {name}'\n"
        f"git -C '{worktree}' push -u origin '{branch}'\n"
        f"date +%s%N > {finished}\n",
        encoding="utf-8",
    )
    worker.chmod(0o700)
    tmux("new-window", "-d", "-t", session, "-n", name, str(worker))
    return branch, worktree, started, finished


def merge_branch(worktree: Path, branch: str) -> None:
    git(worktree, "merge", "--no-edit", "--no-ff", branch)


def run_stage(
    repo: Path,
    work_root: Path,
    api: ForgejoApi,
    session: str,
    name: str,
    nested: bool = False,
) -> tuple[str, Path, int, str]:
    base = "main"
    leaf_names = (f"{name}.leaf-a", f"{name}.leaf-b")
    leaf_specs = [
        start_leaf(repo, work_root, session, leaf, base, f"src/{leaf}.txt")
        for leaf in leaf_names
    ]
    for _, _, started, _ in leaf_specs:
        wait_for(started)
    started_at = [float(started.read_text().strip()) for _, _, started, _ in leaf_specs]
    if max(started_at) - min(started_at) > 150_000_000:
        raise HarnessError(f"same-order leaves did not overlap: {leaf_names}")
    for _, _, _, finished in leaf_specs:
        wait_for(finished)

    branch = f"main.{name}"
    aggregate = work_root / "worktrees" / f"aggregate-{name}"
    git(repo, "worktree", "add", "-b", branch, str(aggregate), base)
    for leaf_branch, _, _, _ in leaf_specs:
        merge_branch(aggregate, leaf_branch)
    git(aggregate, "push", "-u", "origin", branch)

    if nested:
        nested_session = f"{session}-nested"
        tmux("new-session", "-d", "-s", nested_session, "sleep", "300")
        try:
            nested_specs = [
                start_leaf(
                    repo,
                    work_root,
                    nested_session,
                    f"alpha.nested-{suffix}",
                    branch,
                    f"nested/{suffix}.txt",
                )
                for suffix in ("one", "two")
            ]
            for _, _, started, _ in nested_specs:
                wait_for(started)
            for _, _, _, finished in nested_specs:
                wait_for(finished)
            for leaf_branch, _, _, _ in nested_specs:
                merge_branch(aggregate, leaf_branch)
            git(aggregate, "push", "origin", branch)
        finally:
            tmux("kill-session", "-t", nested_session)

    head_sha = git(aggregate, "rev-parse", "HEAD")
    pr = api.create_pr(name, branch, base)
    if pr["head"]["sha"] != head_sha:
        raise HarnessError(f"Forgejo head differs from local aggregate for {name}")
    return branch, aggregate, int(pr["number"]), head_sha


def merge_candidate(
    repo: Path,
    api: ForgejoApi,
    name: str,
    branch: str,
    number: int,
    expected_head: str,
    base_before: str,
) -> None:
    pr = api.get_pr(number)
    head = pr.get("head", {}).get("sha")
    if head != expected_head:
        raise HarnessError(f"{name} head changed without an owner repair")
    reviews = api.reviews(number)
    if len(reviews) != 1 or reviews[0].get("state", "").upper() not in {"APPROVED", "APPROVE"}:
        raise HarnessError(f"{name} has duplicate or missing review evidence: {reviews!r}")
    if reviews[0].get("commit_id") not in {None, expected_head}:
        raise HarnessError(f"{name} review is bound to the wrong head")
    if api.status(expected_head) not in {"success", "neutral"}:
        raise HarnessError(f"{name} CI is not successful")
    base_after = git(repo, "rev-parse", "main")
    if name == "beta" and base_after == base_before:
        raise HarnessError("second ordered candidate did not observe the advanced base")
    if api.mock:
        merge_branch(repo, branch)
        git(repo, "push", "origin", "main")
    api.merge(number)
    if not api.mock:
        git(repo, "fetch", "origin", "main")
        git(repo, "reset", "--hard", "origin/main")


def assert_clean_result(repo: Path, api: ForgejoApi, prs: dict[str, tuple[int, str]]) -> None:
    if len(prs) != len(set(prs)):
        raise HarnessError("duplicate aggregate PR ownership")
    if len(api.list_open_prs()) != 0:
        raise HarnessError("merged ordered candidates remain open")
    if git(repo, "show", "main:src/alpha.leaf-a.txt") != "alpha.leaf-a contribution":
        raise HarnessError("alpha leaf was not integrated")
    if git(repo, "show", "main:src/beta.leaf-b.txt") != "beta.leaf-b contribution":
        raise HarnessError("beta leaf was not integrated")
    if git(repo, "show", "main:nested/one.txt") != "alpha.nested-one contribution":
        raise HarnessError("nested ordered stage was not integrated")
    merge_count = len(git(repo, "log", "--merges", "--format=%H").splitlines())
    if merge_count != 8:
        raise HarnessError(f"expected four leaf and four aggregate/nested merge commits, got {merge_count}")
    for number, expected_head in prs.values():
        pr = api.get_pr(number)
        if pr.get("state") not in {"closed", "merged"}:
            raise HarnessError(f"PR #{number} was not closed by its one merge")
        if pr.get("head", {}).get("sha") != expected_head:
            raise HarnessError(f"PR #{number} lost its authoritative head evidence")


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
    tmux("new-session", "-d", "-s", session, "sleep", "300")
    worktrees: list[Path] = []
    try:
        alpha_branch, alpha_worktree, alpha_pr, alpha_head = run_stage(
            repo, work_root, api, session, "alpha", nested=True
        )
        beta_branch, beta_worktree, beta_pr, beta_head = run_stage(
            repo, work_root, api, session, "beta"
        )
        worktrees.extend((alpha_worktree, beta_worktree))
        prs = {"alpha": (alpha_pr, alpha_head), "beta": (beta_pr, beta_head)}
        if {pr["number"] for pr in api.list_open_prs()} != {alpha_pr, beta_pr}:
            raise HarnessError("same-order aggregate PRs were not both visible")

        # Beta becomes ready first. The parent still integrates by normalized
        # stage order, not by review arrival order.
        api.approve(beta_pr, beta_head)
        api.approve(alpha_pr, alpha_head)
        api.status(beta_head)
        api.status(alpha_head)
        base_before = git(repo, "rev-parse", "main")
        merge_candidate(repo, api, "alpha", alpha_branch, alpha_pr, alpha_head, base_before)
        advanced_base = git(repo, "rev-parse", "main")
        if advanced_base == base_before:
            raise HarnessError("first ordered merge did not advance the parent base")
        if api.get_pr(beta_pr).get("head", {}).get("sha") != beta_head:
            raise HarnessError("base movement changed the second candidate head")
        merge_candidate(repo, api, "beta", beta_branch, beta_pr, beta_head, base_before)
        assert_clean_result(repo, api, prs)
        print(
            json.dumps(
                {
                    "passed": True,
                    "same_order": ["alpha", "beta"],
                    "ready_order": ["beta", "alpha"],
                    "aggregate_prs": prs,
                    "nested_stage": ["alpha.nested-one", "alpha.nested-two"],
                    "merge_count": 8,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        for worktree in worktrees:
            if worktree.exists():
                git(repo, "worktree", "remove", "--force", str(worktree))
        tmux("kill-session", "-t", session)


if __name__ == "__main__":
    try:
        main()
    except (HarnessError, KeyError) as error:
        raise SystemExit(f"ordered recursive Forgejo E2E failed: {error}") from error
