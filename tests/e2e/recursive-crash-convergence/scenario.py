"""The synthetic recreate scenario for #1117: PR A -> recreate -> PR B.

The scenario reproduces the shape the recreated-publication bug was found in,
without any captured material: a disposable project on this run's own Forgejo
publishes PR A through the shipped controller, the run is confirmed recreated
with the shipped ``--recreate --confirm-recreate`` path, a new dispatch follows,
and the leaf publishes PR B. Each item then proves one property of that shape
against durable evidence -- the forge's own pull-request listing, the committed
ledger, git's worktree registry, the active and archived checkpoints, and the
run's own Chainlink database.

Everything runs through the shipped binary: ``exomonad new``, ``exomonad
init --start``, and ``exomonad init --recreate --confirm-recreate``. The leaf is
the deterministic actor beside this file, so no model call is spent.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import e2e_harness.cleanup as cl  # noqa: E402
import e2e_harness.forgejo_stack as fj  # noqa: E402
import e2e_harness.tmuxio as tmuxio  # noqa: E402
from e2e_harness.waiter import await_boundary  # noqa: E402

from run_prefix import CHILD_SUB_TL, LEAF_SLICE, LEGS  # noqa: E402

#: Where the harness reads the shared artifacts from.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
HARNESS_DIR = Path(__file__).resolve().parent

#: The deterministic leaf actor, the same one the crash matrix uses.
LEAF_ACTOR = HARNESS_DIR / "leaf_publication_agent.py"

#: The controller interpreter. ``exomonad init`` resolves the controller archive
#: from ``$HOME/.exo/tl_loop.pyz`` and refuses to fall back, so the run supplies
#: the archive its own build produced instead of the operator's installed one.
CONTROLLER_INTERPRETER = Path(
    os.environ.get(
        "EXOMONAD_TL_LOOP_PYTHON",
        "/home/goya/agent-workspace/exomonad/tl_loop/.venv/bin/python",
    )
)

#: The archive this worktree's build produced, embedded in the binary anyway.
def controller_archive() -> Path:
    """Return the controller archive this worktree's build produced."""
    built = sorted(
        (PROJECT_ROOT / "target" / "debug" / "build").glob("*/out/tl_loop.pyz")
    )
    if not built:
        raise ScenarioError(
            "no built controller archive under target/debug/build/*/out/tl_loop.pyz"
        )
    return built[-1]


class ScenarioError(RuntimeError):
    """Raised when an item's own acceptance assertion does not hold."""


def require(condition: bool, message: str) -> None:
    """Fail the current item unless the condition holds."""
    if not condition:
        raise ScenarioError(message)


@dataclass
class Project:
    """One disposable project, its session, and the server ``init`` started."""

    scope: cl.RunScope
    instance: fj.Instance
    database: Path
    repo: Path
    home: Path
    bin_dir: Path
    log_dir: Path
    session: str
    leaf_branch: str
    environment: dict[str, str] = field(default_factory=dict)

    @property
    def ledger_path(self) -> Path:
        return self.repo / ".exo" / "ledger" / "segments"

    def stop_for_restart(self) -> None:
        """Stop the run's tmux session, and so its server and controller.

        This is what restarting means here: the session and the processes in it
        are gone while the project, its repository, its ledger, and its
        checkpoint survive, so the next ``init`` starts a server and a
        controller against durable state rather than against nothing. The
        session is killed through the run's own socket, so it can only ever
        reach this run's server.
        """
        tmuxio.tmux(self.scope.tmux_socket, "kill-session", "-t", self.session)

    def ledger(self) -> list[dict[str, Any]]:
        """Read every committed ledger record for this project."""
        events: list[dict[str, Any]] = []
        if not self.ledger_path.is_dir():
            return events
        for path in sorted(self.ledger_path.glob("*")):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    events.append(value)
        return events

    def typed(self, event_type: str) -> list[dict[str, Any]]:
        return [e for e in self.ledger() if e.get("type") == event_type]

    def active_run(self) -> Path:
        return self.repo / ".exo" / "tl-loop" / "root"

    def archives(self) -> list[Path]:
        return sorted(
            (self.repo / ".exo" / "tl-loop").glob("root.invalid-*")
        )


# --------------------------------------------------------------------------
# Project bootstrap
# --------------------------------------------------------------------------


def _run(
    command: Sequence[str], cwd: Path, env: Mapping[str, str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(env),
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        raise ScenarioError(
            f"{' '.join(command[:3])} failed ({result.returncode}): "
            f"{(result.stderr or result.stdout)[-2000:]}"
        )
    return result


def _git(project: Project, *arguments: str) -> str:
    return _run(
        ["git", *arguments], project.repo, project.environment
    ).stdout.strip()


def _write_config(project: Project) -> None:
    """Write the project config the shipped server reads."""
    (project.repo / ".exo").mkdir(parents=True, exist_ok=True)
    (project.repo / ".exo" / "config.toml").write_text(
        "\n".join(
            [
                'default_role = "devswarm"',
                'wasm_name = "devswarm"',
                'shell_command = "bash"',
                f'tmux_session = "{project.session}"',
                "yolo = true",
                "poll_interval = 1",
                'root_agent_type = "codex"',
                'spawn_agent_type = "codex"',
                'reviewer_agent_type = "codex"',
                f'forgejo_url = "{project.instance.base_url}"',
                f'forgejo_token = "{project.instance.author.token}"',
                f'forgejo_reviewer_token = "{project.instance.reviewer.token}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _leaf_branch(leg: str) -> str:
    """Return the branch the shipped spawn path gives this leg's leaf.

    The shipped spawn path derives a leaf branch from its owning scope
    (``<scope branch>.<slice>-<agent type>``), so the ``child`` leg's leaf
    hangs off the child sub-TL's branch instead of ``main``.
    """
    scope = f"main.{CHILD_SUB_TL}" if leg == "child" else "main"
    return f"{scope}.{LEAF_SLICE}-codex"


def _plan_document(leg: str) -> dict[str, Any]:
    """Return the closed-key WorkPlan document this leg launches with."""
    leaf = {"name": LEAF_SLICE, "task": "Publish the recreated publication leaf"}
    if leg != "child":
        return {"run_id": "root", "plan": {"leaves": [leaf]}}
    return {
        "run_id": "root",
        "plan": {
            "sub_tls": [
                {
                    "name": CHILD_SUB_TL,
                    "plan": {"leaves": [leaf]},
                }
            ]
        },
    }


def _write_plan(project: Project, leg: str) -> None:
    """Write the WorkPlan the controller is launched with."""
    plan_dir = project.repo / ".exo" / "tl-loop"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan.json").write_text(
        json.dumps(_plan_document(leg)) + "\n",
        encoding="utf-8",
    )


def _write_agent_shim(project: Project) -> None:
    """Write the shims every agent type resolves to.

    The server spawns agents by name from the tmux session's ``PATH``, so each
    agent type gets a shim that hands control to the deterministic leaf actor
    with the socket and the leaf's own branch already set. Resolving *every*
    agent type through the same shim is what keeps a real agent binary from ever
    being started against a disposable repository.
    """
    project.bin_dir.mkdir(parents=True, exist_ok=True)
    for agent_type in ("codex", "claude", "opencode"):
        shim = project.bin_dir / agent_type
        shim.write_text(
            "#!/bin/sh\n"
            f"export EXOMONAD_SOCKET={shlex.quote(str(project.repo / '.exo' / 'server.sock'))}\n"
            f"export EXOMONAD_1057_LEAF_BRANCHES={shlex.quote(project.leaf_branch)}\n"
            "# This run's approval comes from the acceptance's own `review` item:\n"
            "# a spawned reviewer resolving through this shim would post a second\n"
            "# approval for the same head. The spawn still happens; only the\n"
            "# duplicate submission belongs to the harness.\n"
            "export EXOMONAD_REVIEW_OWNED_BY_HARNESS=1\n"
            f"exec {shlex.quote(str(CONTROLLER_INTERPRETER))} "
            f"{shlex.quote(str(LEAF_ACTOR))} \"$@\"\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)


def bootstrap(
    scope: cl.RunScope,
    instance: fj.Instance,
    database: Path,
    session: str,
    leg: str = "recreate",
) -> Project:
    """Create the disposable project this scenario runs its server against."""
    repo = scope.root / "repo"
    home = scope.root / "home"
    (home / ".exo").mkdir(parents=True, exist_ok=True)
    shutil.copy2(controller_archive(), home / ".exo" / "tl_loop.pyz")
    project = Project(
        scope=scope,
        instance=instance,
        database=database,
        repo=repo,
        home=home,
        bin_dir=scope.root / "bin",
        log_dir=scope.root / "logs",
        session=session,
        leaf_branch=_leaf_branch(leg),
        environment=_environment(scope, instance, session, database),
    )
    project.log_dir.mkdir(parents=True, exist_ok=True)
    # ``exomonad init`` starts the run's tmux server with a bare tmux call that
    # will not create the socket's directory, so it has to exist first.
    tmuxio.ensure(tmuxio.socket_path(scope.root))
    _write_agent_shim(project)
    _seed_repository(project)
    _write_config(project)
    _write_plan(project, leg)
    return project


def _environment(
    scope: cl.RunScope, instance: fj.Instance, session: str, database: Path
) -> dict[str, str]:
    """Return the environment every ``exomonad init`` runs under.

    ``HOME`` is the run's own directory, because the shipped controller
    resolution reads ``$HOME/.exo/tl_loop.pyz`` and this harness must exercise
    the archive its own build produced rather than the operator's installed one.

    The rest comes from ``tmuxio.child_env``: ``TMUX`` and ``TMUX_PANE`` are
    removed so the shipped binary's own bare tmux calls cannot resolve an outer
    server, and ``TMUX_TMPDIR`` points at the run's directory so they land on
    the run's socket instead. The run's ``PATH`` comes first, which is what
    keeps a disposable repository out of a real agent binary.
    """
    return tmuxio.child_env(
        scope.root,
        {
            **os.environ,
            "HOME": str(scope.root / "home"),
            "PATH": f"{scope.root / 'bin'}:{os.environ.get('PATH', '')}",
            "EXOMONAD_TL_LOOP_PYTHON": str(CONTROLLER_INTERPRETER),
            "CHAINLINK_DB": str(database),
        },
    )


def _seed_repository(project: Project) -> None:
    """Clone the run's own repository and commit the scaffolding into it.

    The repository is cloned rather than invented: ``auto_init`` gave the fresh
    Forgejo repository a commit of its own, so a locally created history would
    be a divergent one and the first push would be rejected. Cloning is also
    what makes the base branch the acceptance asserts about the one the forge
    actually serves.

    ``exomonad new`` then writes the harness policy files and the agent
    scaffolding, and committing them is what lets the shipped preflight's
    clean-worktree gate pass on the *first* dispatch instead of failing it --
    the same gate a production project has to clear.
    """
    repo = project.repo
    _run(
        ["git", "clone", "--quiet", project.instance.clone_url(), repo.name],
        project.scope.root,
        project.environment,
    )
    _run(["git", "config", "user.name", "1117-acceptance"], repo, project.environment)
    _run(
        ["git", "config", "user.email", "1117@example.invalid"],
        repo,
        project.environment,
    )
    _run(
        [
            "git",
            "config",
            project.instance.extra_header_key(),
            project.instance.author.extra_header(),
        ],
        repo,
        project.environment,
    )
    _run([str(_binary()), "new"], repo, project.environment)
    wasm = PROJECT_ROOT / ".exo" / "wasm" / "wasm-guest-devswarm.wasm"
    if not wasm.is_file():
        raise ScenarioError(f"the WASM guest is missing: {wasm}")
    (repo / ".exo" / "wasm").mkdir(parents=True, exist_ok=True)
    shutil.copy2(wasm, repo / ".exo" / "wasm" / wasm.name)
    # The role contexts are not generated: the shipped `init` looks for
    # `.exo/roles` and tells the operator to copy it in, and a spawn whose role
    # context is missing is refused with `Missing role context for dev`. The
    # worktree's own copy is what a project is meant to have, and `.exo/` is
    # ignored so it never dirties the clean-worktree gate.
    roles = PROJECT_ROOT / ".exo" / "roles"
    if not roles.is_dir():
        raise ScenarioError(f"the role contexts are missing from this worktree: {roles}")
    shutil.copytree(roles, repo / ".exo" / "roles", dirs_exist_ok=True)
    # Every entry here is runtime scaffolding the shipped preflight already
    # treats as outside the project. Committing the same list is what keeps a
    # leaf worktree clean after a spawn writes into it -- and a dirty leaf
    # worktree is exactly what gates ``--recreate``.
    (repo / ".gitignore").write_text(
        "\n".join(
            [
                ".exo/",
                ".chainlink/",
                ".codex/",
                ".opencode/",
                "opencode.json",
                ".mcp.json",
                ".claude/settings.local.json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _run(["git", "add", "-A"], repo, project.environment)
    _run(
        ["git", "commit", "-q", "-m", "Ignore the shipped scaffolding"],
        repo,
        project.environment,
    )
    _run(["git", "push", "-q", "origin", "main"], repo, project.environment)


def _binary() -> Path:
    """Return this worktree's build, never an operator's installed binary."""
    candidate = Path(
        os.environ.get("EXOMONAD_E2E_BIN", PROJECT_ROOT / "target/debug/exomonad")
    )
    if not candidate.is_file():
        raise ScenarioError(
            f"build target/debug/exomonad first, or set EXOMONAD_E2E_BIN: {candidate}"
        )
    return candidate.resolve()


def run_init(project: Project, *mode: str) -> str:
    """Run one shipped ``exomonad init`` and return its combined output.

    ``init`` always ends by attaching to the session, which cannot succeed
    without a TTY, so a successful run exits 1 with that documented message.
    Anything else is a failure, and the caller says so.
    """
    command = [str(_binary()), "init", *mode, "--session", project.session]
    result = subprocess.run(
        command,
        cwd=project.repo,
        env=project.environment,
        text=True,
        capture_output=True,
        check=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    log = project.log_dir / f"init-{'-'.join(mode)}.log"
    log.write_text(output, encoding="utf-8")
    return output if result.returncode == 1 else output + f"\n[exit={result.returncode}]"


def leaf_worktree_status(project: Project) -> str:
    """Return the leaf worktree's git status, or why it could not be read.

    ``--recreate`` gates on a dirty leaf worktree, so the status is captured
    before the command runs: a refusal that does not say which file is dirty
    sends the reader to guess.
    """
    worktree = project.repo / ".exo" / "worktrees" / f"{LEAF_SLICE}-codex"
    if not worktree.is_dir():
        return f"<no leaf worktree at {worktree}>"
    result = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() or "<clean>"


def require_attach_failure(output: str, mode: str) -> None:
    """Require the documented non-TTY attach failure, not an earlier error."""
    require(
        "open terminal failed: not a terminal" in output,
        f"exomonad init {' '.join(mode)} did not reach its final attach: "
        f"{output[-1500:]}",
    )
    require(
        "exomonad init failed" not in output,
        f"exomonad init {' '.join(mode)} failed before completing: "
        f"{output[-1500:]}",
    )


__all__ = [
    "Project",
    "ScenarioError",
    "bootstrap",
    "leaf_worktree_status",
    "require",
    "require_attach_failure",
    "run_init",
]
