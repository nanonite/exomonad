"""The disposable project and its real server for the #1111 acceptance.

Everything this module creates lives under the run's own temporary directory,
and every tmux session and process it starts is registered with the run scope
before it is started, so the run's single teardown can hand all of it back. The
server is the shipped ``exomonad serve`` binary invoked by absolute path from
this worktree's build, and the leaf is the deterministic actor beside this file
rather than a real agent binary.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import cleanup as cl
import forgejo as fj

PROJECT_ROOT = Path(__file__).resolve().parents[3]
HARNESS_DIR = Path(__file__).resolve().parent
ORDERED_DIR = PROJECT_ROOT / "tests/e2e/ordered-recursive"

sys.path.insert(0, str(HARNESS_DIR))
sys.path.insert(0, str(ORDERED_DIR))

import real_server_transport as real  # noqa: E402

#: The shared harness's own failure type, re-exported so the driver can treat a
#: failed fixture command as an acceptance failure rather than a crash.
HarnessError = real.HarnessError

from evidence import ledger_events, registered_branches  # noqa: E402
from tl_loop.client.effects import EffectClient  # noqa: E402
from tl_loop.client.transport import TransportClient  # noqa: E402

#: The controller authenticates as this role and name.
CONTROLLER_ROLE = "tl"
CONTROLLER_NAME = "parent"

#: The base branch the controller, the ordered child, and the leaf descend from.
BASE_BRANCH = "main"

#: The ordered child the acceptance provisions before it dispatches the leaf.
CHILD_NAME = "stage-a"
CHILD_BRANCH = f"{BASE_BRANCH}.{CHILD_NAME}"

#: The leaf slice's task name. The shipped spawn path derives the agent's
#: internal name, birth branch, and worktree path from it, so the acceptance
#: names the slice once and reads the derived names back from what the server
#: wrote rather than predicting them.
LEAF_NAME = "leaf"

#: The harness the server spawns, which the internal agent name carries as a
#: suffix.
LEAF_HARNESS = "codex"

#: The leaf's internal agent name, branch, and worktree as the spawn path
#: derives them for this slice name and harness.
LEAF_AGENT = f"{LEAF_NAME}-{LEAF_HARNESS}"
LEAF_BRANCH = f"{BASE_BRANCH}.{LEAF_AGENT}"

#: Bounded wait for the server to answer its own tool route.
SERVER_READY_TIMEOUT_SECONDS = 180.0


class ProjectError(RuntimeError):
    """Raised when the disposable project or server cannot be prepared."""


def exomonad_binary() -> Path:
    """Return the debug binary built from this worktree, or refuse to continue.

    The absolute path matters: the acceptance must exercise this worktree's
    build, never an operator's installed binary.
    """
    candidate = Path(
        os.environ.get("EXOMONAD_E2E_BIN", PROJECT_ROOT / "target/debug/exomonad")
    )
    if not candidate.is_file():
        raise ProjectError(
            f"build target/debug/exomonad first, or set EXOMONAD_E2E_BIN: {candidate}"
        )
    return candidate.resolve()


def wasm_plugin() -> Path:
    """Return the WASM guest built from this worktree, or refuse to continue."""
    candidate = Path(
        os.environ.get(
            "EXOMONAD_E2E_WASM", PROJECT_ROOT / ".exo/wasm/wasm-guest-devswarm.wasm"
        )
    )
    if not candidate.is_file():
        raise ProjectError(
            f"run `just wasm devswarm` first, or set EXOMONAD_E2E_WASM: {candidate}"
        )
    return candidate.resolve()


def session_name(scope: cl.RunScope, role: str) -> str:
    """Return the unique tmux session name this run uses for one role.

    Recreating the session reuses the same name, because what the acceptance
    recreates is the session itself; the suffix distinguishes it from another
    run's identically named session.
    """
    return scope.track_session(f"{scope.session_prefix}{role}")


def kill_session(session: str) -> None:
    """Kill exactly one named session, reporting no error when it is gone."""
    subprocess.run(
        ["tmux", "kill-session", "-t", session],
        check=False,
        capture_output=True,
        text=True,
    )


def session_exists(session: str) -> bool:
    """Report whether a named tmux session exists."""
    return (
        subprocess.run(
            ["tmux", "has-session", "-t", session],
            check=False,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


@dataclass(frozen=True)
class Run:
    """The run-scoped settings every server start for this project needs.

    These are held here rather than read back out of the project config, so a
    recreated session is provably configured from this run's own values and
    never from whatever happens to be on disk.
    """

    scope: cl.RunScope
    root: Path
    repo: Path
    session: str
    chainlink_db: Path
    instance: fj.Instance
    leaf_branches: tuple[str, ...]

    @property
    def forgejo_url(self) -> str:
        return self.instance.base_url


@dataclass
class Project:
    """One disposable project with its real server and transport client.

    The handle is mutable because the acceptance recreates the session
    mid-run: a scenario that recreates it rebinds the handle to the new server
    so every later item runs against the session that is actually live.
    """

    run: Run
    port: int
    process: subprocess.Popen[str]
    client: TransportClient

    @property
    def repo(self) -> Path:
        return self.run.repo

    @property
    def session(self) -> str:
        return self.run.session

    @property
    def instance(self) -> fj.Instance:
        return self.run.instance

    def effects(self, name: str = CONTROLLER_NAME) -> EffectClient:
        """Return an effect client for one agent on this server."""
        return EffectClient(self.client, role=CONTROLLER_ROLE, name=name)

    def ledger(self) -> list[dict[str, Any]]:
        """Read this project's committed ledger records."""
        return ledger_events(self.repo)

    def run_id(self) -> str:
        """Return the swarm UUID the running server minted."""
        return real.server_run_id(self.repo)

    def close(self) -> None:
        """Stop this run's server and remove this run's session.

        The process is registered with the run scope, so a teardown that never
        reaches here still reclaims it; stopping it here keeps the recreated
        session from racing the process that held the port.
        """
        try:
            real.stop_subprocess(self.process, "acceptance server")
        finally:
            kill_session(self.run.session)


def _clone(root: Path, instance: fj.Instance) -> Path:
    """Clone the run's own repository into the run's own directory.

    The credential is configured for this repository only, so it never reaches
    a shared global config, another worktree, or the remote URL printed as
    evidence.
    """
    repo = root / "repo"
    result = subprocess.run(
        ["git", "clone", "--quiet", instance.clone_url(), str(repo)],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise ProjectError(
            f"could not clone the acceptance repository: {result.stderr.strip()}"
        )
    real.run_command(
        ["git", "-C", str(repo), "config", instance.extra_header_key(), instance.author.extra_header()]
    )
    real.git(repo, "config", "user.name", "e2e-1111-author")
    real.git(repo, "config", "user.email", "e2e-1111-author@example.invalid")
    real.git(repo, "checkout", "--quiet", BASE_BRANCH)
    _ignore_exomonad_scaffolding(repo)
    return repo


def _ignore_exomonad_scaffolding(repo: Path) -> None:
    """Commit an ignore rule for the server's own scaffolding.

    The shipped spawn preflight refuses to spawn out of a dirty worktree, and
    the server writes its scaffolding into the project. Ignoring it is what
    makes the project clean by construction rather than by suppressing the
    preflight, so the acceptance runs against the same gate production does.
    """
    (repo / ".gitignore").write_text(".exo/\n.chainlink/\n", encoding="utf-8")
    real.git(repo, "add", ".gitignore")
    real.git(repo, "commit", "-q", "-m", "Ignore ExoMonad scaffolding")
    real.git(repo, "push", "-q", "origin", BASE_BRANCH)


def _fake_agent_bin(run: Run) -> Path:
    """Write the shim that stands in for an agent binary.

    The server spawns the configured agent type by name, so this shim is what
    the leaf becomes. It exports the socket the real spawn path provides and
    hands control to the deterministic actor beside this file, so the leaf
    spends no model call while still committing, pushing, and filing a pull
    request through the shipped tool surface.
    """
    fake_bin = run.root / "fake-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    launcher = fake_bin / LEAF_HARNESS
    launcher.write_text(
        "#!/bin/sh\n"
        f"export EXOMONAD_SOCKET={shlex.quote(str(run.repo / '.exo' / 'server.sock'))}\n"
        f"export EXOMONAD_1111_LEAF_BRANCHES="
        f"{shlex.quote(','.join(sorted(run.leaf_branches)))}\n"
        f"exec {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(HARNESS_DIR / 'leaf_agent.py'))} \"$@\"\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return fake_bin


def _write_config(run: Run, port: int) -> None:
    """Write the project config the real server reads."""
    (run.repo / ".exo").mkdir(parents=True, exist_ok=True)
    (run.repo / ".exo/config.toml").write_text(
        "\n".join(
            [
                'default_role = "devswarm"',
                'wasm_name = "devswarm"',
                'wasm_dir = ".exo/wasm"',
                'project_dir = "."',
                f'tmux_session = "{run.session}"',
                f"port = {port}",
                "yolo = true",
                f'spawn_agent_type = "{LEAF_HARNESS}"',
                f'forgejo_url = "{run.forgejo_url}"',
                f'forgejo_token = "{run.instance.author.token}"',
                f'forgejo_reviewer_token = "{run.instance.reviewer.token}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _provision_controller_worktree(run: Run) -> None:
    """Create the controller worktree the acceptance authenticates as.

    The ordered child is deliberately *not* pre-created: the server's
    ``provision_ordered_sub_tl`` route is the shipped boundary that decides
    whether a child is new, reattaches to an existing worktree, or refuses an
    ownership conflict, and it derives the child's own worktree itself. A
    pre-made child worktree would make that decision unreachable.
    """
    worktree = run.repo / ".exo" / "worktrees" / CONTROLLER_NAME
    worktree.parent.mkdir(parents=True, exist_ok=True)
    real.run_command(
        [
            "git",
            "-C",
            str(run.repo),
            "worktree",
            "add",
            "-q",
            "-b",
            f"{BASE_BRANCH}.{CONTROLLER_NAME}",
            str(worktree),
            BASE_BRANCH,
        ]
    )
    # The ordered provisioning check requires the caller's resolved birth branch
    # to equal the configured parent branch, and the server resolves
    # identity.json before the worktree's git branch, so the controller's own
    # identity is recorded explicitly.
    identity = {
        "agent_name": CONTROLLER_NAME,
        "slug": CONTROLLER_NAME,
        "agent_type": LEAF_HARNESS,
        "birth_branch": BASE_BRANCH,
        "parent_branch": BASE_BRANCH,
        "working_dir": str(worktree),
        "display_name": f"{CONTROLLER_NAME} controller",
        "topology": "worktree_per_agent",
        "ledger_owned": True,
        "slice_id": CONTROLLER_NAME,
    }
    controller_identity = run.repo / ".exo" / "agents" / CONTROLLER_NAME
    controller_identity.mkdir(parents=True, exist_ok=True)
    (controller_identity / "identity.json").write_text(
        json.dumps(identity, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (controller_identity / ".birth_branch").write_text(
        f"{BASE_BRANCH}\n", encoding="utf-8"
    )


def _start_session(run: Run) -> None:
    """Create this run's tmux session and give it the acceptance's PATH."""
    fake_bin = _fake_agent_bin(run)
    test_path = f"{fake_bin}:{os.environ.get('PATH', '')}"
    real.run_command(
        ["tmux", "new-session", "-d", "-s", run.session, "-n", "TL", "sleep", "600"]
    )
    real.run_command(["tmux", "set-environment", "-t", run.session, "PATH", test_path])
    real.run_command(
        [
            "tmux",
            "set-environment",
            "-t",
            run.session,
            "CHAINLINK_DB",
            str(run.chainlink_db),
        ]
    )


def _serve(run: Run, port: int, log_name: str) -> Project:
    """Start the real server and block until it answers its own tool route."""
    log = (run.root / log_name).open("w", encoding="utf-8")
    test_path = f"{run.root / 'fake-bin'}:{os.environ.get('PATH', '')}"
    process = subprocess.Popen(
        [str(exomonad_binary()), "serve"],
        cwd=run.repo,
        env={**os.environ, "PATH": test_path, "CHAINLINK_DB": str(run.chainlink_db)},
        stdout=log,
        stderr=log,
        text=True,
        # Its own process group, so the run's teardown can signal the whole
        # group without the signal reaching the harness that started it.
        start_new_session=True,
    )
    run.scope.track_process(process, "acceptance server")
    client = TransportClient(project_root=run.repo, timeout=30)
    try:
        real.wait_for_server(client, process, Path(log.name))
    except BaseException:
        real.stop_subprocess(process, "acceptance server startup")
        kill_session(run.session)
        raise
    return Project(run=run, port=port, process=process, client=client)


def new_run(
    scope: cl.RunScope,
    instance: fj.Instance,
    chainlink_db: Path,
    *,
    leaf_branches: Sequence[str] = (),
) -> Run:
    """Clone this run's repository and record what its servers need."""
    root = scope.root
    repo = _clone(root, instance)
    return Run(
        scope=scope,
        root=root,
        repo=repo,
        session=session_name(scope, "session"),
        chainlink_db=chainlink_db,
        instance=instance,
        leaf_branches=tuple(leaf_branches),
    )


def start(run: Run) -> Project:
    """Configure and start a real server over this run's project."""
    port = real.free_port()
    _write_config(run, port)
    (run.repo / ".exo/wasm").mkdir(parents=True, exist_ok=True)
    shutil.copy2(wasm_plugin(), run.repo / ".exo/wasm/wasm-guest-devswarm.wasm")
    _provision_controller_worktree(run)
    _start_session(run)
    return _serve(run, port, "server.log")


def recreate_session(run: Run, port: int) -> Project:
    """Recreate the session and server over the same project.

    This is the recreated-leaf boundary the acceptance is named for: the
    project, its repository, its branches, its forge records, and its ledger
    all survive, while the tmux session and the server process do not.
    """
    _start_session(run)
    return _serve(run, port, "server-recreated.log")


def provision_child(project: Project) -> Mapping[str, Any]:
    """Provision the ordered child through the real server route."""
    project.client.provision_ordered_sub_tl(
        CONTROLLER_NAME,
        agent_name=CHILD_NAME,
        birth_branch=CHILD_BRANCH,
        parent_branch=BASE_BRANCH,
        # The server creates the child's worktree; the request names the
        # deterministic path that ownership is then recorded against, and does
        # not pre-create it.
        working_dir=project.repo / ".exo" / "worktrees" / CHILD_NAME,
        slice_id=CHILD_NAME,
    )
    child_worktree = (project.repo / ".exo" / "worktrees" / CHILD_NAME).resolve()
    registered = registered_branches(project.repo)
    if registered.get(child_worktree) != CHILD_BRANCH:
        raise ProjectError(
            f"the server did not register the ordered child worktree: "
            f"{registered.get(child_worktree)!r} is not {CHILD_BRANCH!r}"
        )
    return {"child_worktree": str(child_worktree), "child_branch": CHILD_BRANCH}


__all__ = [
    "BASE_BRANCH",
    "HarnessError",
    "CHILD_BRANCH",
    "CHILD_NAME",
    "CONTROLLER_NAME",
    "LEAF_AGENT",
    "LEAF_BRANCH",
    "LEAF_HARNESS",
    "LEAF_NAME",
    "Project",
    "ProjectError",
    "Run",
    "kill_session",
    "new_run",
    "provision_child",
    "recreate_session",
    "session_exists",
    "session_name",
    "start",
]
