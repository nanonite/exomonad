"""Build the scratch repository a CI typecheck run executes against.

The repository is a *copy* of this project, never a hand-written lookalike:
the typecheck job comes from the real ``.forgejo/workflows/ci.yml`` and the
``tl-loop-typecheck`` recipe comes from the real ``justfile``. Extracting them
means a change to the job the project actually runs is the thing the harness
verifies, and a second copy of the job cannot drift from the first.

Only the files the job needs are copied: the ``tl_loop`` package, the ratchet
script it invokes, the extracted recipe, and the extracted workflow.
"""

from __future__ import annotations

import shutil
from pathlib import Path

#: The workflow file the real project publishes.
CI_WORKFLOW = Path(".forgejo") / "workflows" / "ci.yml"

#: The job this acceptance runs.
JOB_NAME = "typecheck"

#: The recipe the job invokes.
RECIPE_NAME = "tl-loop-typecheck"

#: Scripts the copied package imports. ``test_build_archive`` imports the two
#: archive scripts, so mypy checks them through that test; the ratchet script is
#: what the recipe invokes.
SCRIPTS_NEEDED = (
    "check_typecheck_ratchet.py",
    "build_tl_loop_archive.py",
    "check_tl_loop_archive.py",
)

#: The strict error injected for the red run. It is a new file, so its error is
#: a new (path, code) pair in a baseline that starts empty: exactly the growth
#: the ratchet exists to reject.
PROBE_RELATIVE_PATH = Path("tl_loop") / "ratchet_probe.py"

PROBE_SOURCE = '''"""A strict mypy error injected by the typecheck CI acceptance."""


def ratchet_probe(value: int) -> str:
    return value
'''

#: Directories inside the copied package that are working state, not source.
_IGNORED_PACKAGE_DIRS = {"__pycache__", ".venv", ".pytest_cache", ".ruff_cache"}


class ScratchError(RuntimeError):
    """Raised when the scratch repository cannot be built as specified."""


def typecheck_job(ci_text: str) -> str:
    """Return the real ``jobs.typecheck`` block, with its own indentation.

    The block is copied verbatim, including its steps and environment, so the
    acceptance cannot silently run a simplified job. Parsing is line-oriented
    on purpose: the checked-in workflow is a small, regular YAML document, and
    a parser that reconstructs it would no longer prove the real bytes ran.
    """
    lines = ci_text.splitlines()
    try:
        jobs_at = lines.index("jobs:")
    except ValueError as error:
        raise ScratchError(f"{CI_WORKFLOW} has no top-level jobs: block") from error
    marker = f"  {JOB_NAME}:"
    start: int | None = None
    for index in range(jobs_at + 1, len(lines)):
        line = lines[index]
        if line == marker:
            start = index
            break
        if line and not line.startswith(" "):
            break
    if start is None:
        raise ScratchError(f"{CI_WORKFLOW} has no jobs.{JOB_NAME} block")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith("    "):
            end = index
            break
    block = lines[start:end]
    while block and not block[-1].strip():
        block.pop()
    return "\n".join(block)


def render_workflow(ci_text: str) -> str:
    """Return a workflow made of the real typecheck job and nothing else."""
    return (
        "name: typecheck-ci-e2e\n"
        "\n"
        "on: push\n"
        "\n"
        "jobs:\n"
        f"{typecheck_job(ci_text)}\n"
    )


def just_variable(justfile_text: str, name: str = "py") -> str:
    """Return a top-level ``name := ...`` assignment from the real justfile."""
    prefix = f"{name} :="
    for line in justfile_text.splitlines():
        if line.startswith(prefix):
            return line
    raise ScratchError(f"the justfile has no top-level {name!r} assignment")


def recipe(justfile_text: str, name: str = RECIPE_NAME) -> str:
    """Return one recipe from the real justfile, with its body and comments.

    A recipe starts at an unindented ``name:`` line and ends at the next
    unindented line. Comments immediately above it are kept only when they are
    part of the same block, so the extracted recipe reads like the original.
    """
    lines = justfile_text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if (
            line == f"{name}:" or line.startswith(f"{name} ")
        ) and not line.startswith(" "):
            start = index
            break
    if start is None:
        raise ScratchError(f"the justfile has no {name!r} recipe")
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line and not line.startswith((" ", "\t")):
            break
        end += 1
    block = lines[start:end]
    while block and not block[-1].strip():
        block.pop()
    return "\n".join(block)


def render_justfile(justfile_text: str) -> str:
    """Return a justfile carrying only what the typecheck recipe needs."""
    return f"{just_variable(justfile_text)}\n\n{recipe(justfile_text)}\n"


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in _IGNORED_PACKAGE_DIRS or name.endswith((".pyc", ".pyo"))
    }


def write_scratch(project_root: Path, destination: Path) -> Path:
    """Populate ``destination`` with the generic project the job typechecks."""
    if destination.exists():
        raise ScratchError(f"scratch destination already exists: {destination}")
    destination.mkdir(parents=True)

    shutil.copytree(project_root / "tl_loop", destination / "tl_loop", ignore=_ignore)
    scripts = destination / "scripts"
    scripts.mkdir()
    for script in SCRIPTS_NEEDED:
        shutil.copy2(
            project_root / "scripts" / script,
            scripts / script,
        )

    ci_text = (project_root / CI_WORKFLOW).read_text(encoding="utf-8")
    workout = destination / CI_WORKFLOW
    workout.parent.mkdir(parents=True)
    workout.write_text(render_workflow(ci_text), encoding="utf-8")

    justfile_text = (project_root / "justfile").read_text(encoding="utf-8")
    (destination / "justfile").write_text(
        render_justfile(justfile_text), encoding="utf-8"
    )
    return destination


def inject_strict_error(destination: Path) -> Path:
    """Add one new strict mypy error to the scratch copy and return its path."""
    path = destination / PROBE_RELATIVE_PATH
    if path.exists():
        raise ScratchError(f"the ratchet probe already exists: {path}")
    path.write_text(PROBE_SOURCE, encoding="utf-8")
    return path
