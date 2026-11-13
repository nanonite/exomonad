"""Contract checks for the disposable Actions template (spike #1337).

These run without Docker: they read the checked-in template and assert the
properties the disposable nested-docker stack depends on. A regression here
would either make two runs collide (a fixed container name or host port), break
the offline job path (a dind image that cannot copy ``/var/run/act``, a pull
policy that contacts a registry), or silently drop the host mapping that lets a
job container reach Forgejo.

Run them with the same interpreter the e2e recipes use:

    tl_loop/.venv/bin/python -m pytest -q tests/e2e/lib/forgejo/test_contract.py
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parent
COMPOSE = TEMPLATE_DIR / "docker-compose.actions.yml"
RUNNER_CONFIG = TEMPLATE_DIR / "runner-config.yml"
ENTRYPOINT = TEMPLATE_DIR / "runner-entrypoint.sh"


def _specification(path: Path) -> str:
    """Return the file's specification with its prose removed.

    The properties below are about what the file declares, so the comments that
    describe them must not be able to satisfy an assertion on their own.
    """
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def _section(specification: str, name: str) -> list[str]:
    """Return the indented lines of one top-level ``name:`` block."""
    lines = specification.splitlines()
    try:
        start = lines.index(f"{name}:")
    except ValueError:
        return []
    section: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):
            break
        section.append(line)
    return section


def _children(specification: str, name: str) -> dict[str, list[str]]:
    """Return the direct children of a top-level block, keyed by child name."""
    children: dict[str, list[str]] = {}
    current: str | None = None
    for line in _section(specification, name):
        if not line.startswith(" "):
            continue
        if not line.startswith("   ") and line.rstrip().endswith(":"):
            current = line.strip()[:-1]
            children[current] = []
        elif current is not None:
            children[current].append(line)
    return children


def _compose() -> tuple[str, dict[str, list[str]]]:
    specification = _specification(COMPOSE)
    return specification, _children(specification, "services")


def _entrypoint_source() -> str:
    return ENTRYPOINT.read_text(encoding="utf-8")


def _resolution_guard(source: str) -> str:
    """Return the entrypoint's own forgejo-resolution guard as runnable ``sh``."""
    start = source.index('forgejo_ip="$(getent hosts forgejo')
    end = source.index("\nfi\n", start) + len("\nfi\n")
    return source[start:end]


def _awk_patch_program(source: str) -> str:
    """Return the entrypoint's own awk program that patches the config."""
    match = re.search(
        r"""awk -v options="\$options" '\n(?P<program>.*?)\n' "\$CONFIG\"""",
        source,
        re.DOTALL,
    )
    assert match is not None, "the entrypoint must patch container.options with awk"
    return match.group("program")


def test_the_template_has_no_fixed_container_name_or_host_port() -> None:
    """A fixed name or port would make two runs collide and answer each other."""
    specification, services = _compose()
    assert "container_name" not in specification
    assert set(services) == {"forgejo", "dind", "runner"}
    forgejo = "\n".join(services["forgejo"])
    assert "ports:" in forgejo
    assert '"127.0.0.1::3000"' in forgejo
    for line in forgejo.splitlines():
        if "127.0.0.1" in line:
            assert "::" in line, "a fixed host port would make two runs collide"


def test_the_template_pins_a_privileged_dind_28_on_an_internal_network() -> None:
    """Docker 29 cannot copy the runner payload; dind must stay privileged."""
    specification, services = _compose()
    dind = "\n".join(services["dind"])
    assert "image: docker:28-dind" in dind
    assert "privileged: true" in dind
    assert "networks: [ci]" in dind
    ci = "\n".join(_children(specification, "networks")["ci"])
    assert "internal: true" in ci


def test_the_template_runner_registers_through_the_entrypoint() -> None:
    """The host mapping and registration must come from the checked-in files."""
    _, services = _compose()
    runner = "\n".join(services["runner"])
    assert "./runner-config.yml:/etc/runner-config.yml:ro" in runner
    assert "./runner-entrypoint.sh:/runner-entrypoint.sh:ro" in runner
    assert 'command: ["/bin/sh", "/runner-entrypoint.sh"]' in runner
    assert "${FORGEJO_RUNNER_TOKEN:-}" in runner
    assert "${FORGEJO_RUNNER_LABELS:-" in runner
    assert "DOCKER_HOST=tcp://dind:2375" in runner


def test_the_runner_config_keeps_offline_images_and_disables_the_cache() -> None:
    """force_pull=true would still contact a registry the stack cannot reach."""
    specification = _specification(RUNNER_CONFIG)
    container = "\n".join(_section(specification, "container"))
    assert "force_pull: false" in container
    cache = "\n".join(_section(specification, "cache"))
    assert "enabled: false" in cache


def test_the_entrypoint_patches_container_options() -> None:
    """The job container gets the mapping because the entrypoint writes it."""
    source = _entrypoint_source()
    assert 'forgejo_ip="$(getent hosts forgejo' in source
    assert 'options="--add-host forgejo:${forgejo_ip}"' in source

    config = _specification(RUNNER_CONFIG)
    patched = subprocess.run(
        [
            "awk",
            "-v",
            "options=--add-host forgejo:172.20.0.2",
            _awk_patch_program(source),
        ],
        input=config,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert '  options: "--add-host forgejo:172.20.0.2"' in patched
    assert "force_pull: false" in patched
    assert "enabled: false" in patched


def test_the_entrypoint_refuses_a_config_it_cannot_patch() -> None:
    """An unpatched config would leave job containers unable to reach Forgejo."""
    source = _entrypoint_source()
    patched = subprocess.run(
        ["awk", "-v", "options=x", _awk_patch_program(source)],
        input='container:\n  network: ""\n  force_pull: false\n',
        capture_output=True,
        text=True,
        check=False,
    )
    assert patched.returncode == 2
    assert "error: runner config has no container.options line to patch" in source


def test_the_entrypoint_fails_when_forgejo_cannot_be_resolved(tmp_path: Path) -> None:
    """No resolution means no host mapping, so the job could never reach Forgejo."""
    source = _entrypoint_source()
    guard = _resolution_guard(source)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    stub = bin_dir / "getent"
    stub.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    stub.chmod(0o755)
    unresolved = subprocess.run(
        ["sh", "-c", guard],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        check=False,
    )
    assert unresolved.returncode == 1
    assert "could not resolve forgejo" in unresolved.stderr

    stub.write_text("#!/bin/sh\necho '172.20.0.2 forgejo'\n", encoding="utf-8")
    resolved = subprocess.run(
        ["sh", "-c", guard],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        check=False,
    )
    assert resolved.returncode == 0, resolved.stderr
