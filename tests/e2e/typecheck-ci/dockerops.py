"""Docker operations the typecheck acceptance is allowed to perform.

The host is shared with other projects, so every operation here is either a
read of the host cache, a command on a container this run created, or a
compose command scoped to this run's project name. Nothing reaches a registry:
``pull`` and ``build`` are refused centrally, which is also what keeps a missing
image from being repaired silently instead of failing Phase 0 by name.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

_FORBIDDEN_VERBS = {"pull", "build"}


class DockerError(RuntimeError):
    """Raised when a Docker command the run depends on fails."""


def _refuses_registry(args: Sequence[str]) -> bool:
    if not args:
        return False
    if args[0] in _FORBIDDEN_VERBS:
        return True
    if args[0] == "image" and len(args) > 1 and args[1] in _FORBIDDEN_VERBS:
        return True
    return args[0] == "compose" and any(
        part in _FORBIDDEN_VERBS for part in args[1:]
    )


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one docker command, refusing the two verbs that would use a network."""
    if _refuses_registry(args):
        raise DockerError(
            "refusing to run a registry command; this acceptance uses only "
            f"cached images and never pulls or builds: docker {' '.join(args)}"
        )
    result = subprocess.run(
        ["docker", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode:
        raise DockerError(
            f"docker {' '.join(args)} failed: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def compose(
    project: str,
    compose_file: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one compose command scoped to this run's project."""
    if _refuses_registry(("compose", *args)):
        raise DockerError(
            "refusing to run a compose command that would use a network: "
            f"docker compose {' '.join(args)}"
        )
    result = subprocess.run(
        ["docker", "compose", "-p", project, "-f", str(compose_file), *args],
        check=False,
        capture_output=True,
        text=True,
        env=None if env is None else {**os.environ, **env},
    )
    if result.returncode:
        raise DockerError(
            f"docker compose -p {project} {' '.join(args)} failed: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def service_container(project: str, compose_file: Path, service: str) -> str:
    """Return the container id of one compose service this run created."""
    output = compose(project, compose_file, "ps", "-q", service).stdout.strip()
    if not output:
        raise DockerError(f"compose project {project!r} has no running {service}")
    return output.splitlines()[0]


def load_into_nested(image: str, dind_container: str) -> str:
    """Copy a host-cached image into the run's nested dockerd.

    The nested daemon has its own image store, so a host cache entry is
    invisible to a job until it is saved and loaded. The load is the step that
    makes the run offline; no registry is involved.
    """
    save = subprocess.Popen(
        ["docker", "save", image],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert save.stdout is not None
    load = subprocess.Popen(
        [
            "docker",
            "exec",
            "-i",
            dind_container,
            "docker",
            "-H",
            "tcp://localhost:2375",
            "load",
        ],
        stdin=save.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    save.stdout.close()
    load_out, load_err = load.communicate(timeout=600)
    save_out, save_err = save.communicate(timeout=600)
    if save.returncode:
        raise DockerError(
            f"docker save {image} failed: "
            f"{save_err.decode(errors='replace').strip()}"
        )
    if load.returncode:
        raise DockerError(
            f"docker load into {dind_container} failed: "
            f"{load_err.decode(errors='replace').strip()}"
        )
    text = load_out.decode(errors="replace").strip()
    return text or save_out.decode(errors="replace").strip()


def remove_from_nested(image: str, dind_container: str, timeout: float = 30.0) -> None:
    """Remove one image from the nested store, never from the host cache.

    A job that just finished may still be releasing its container, and the
    nested daemon refuses to remove an image in use. Retrying is bounded and
    turns that short race into a wait instead of a false harness failure.
    """
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        result = docker(
            "exec",
            dind_container,
            "docker",
            "-H",
            "tcp://localhost:2375",
            "rmi",
            image,
            check=False,
        )
        if result.returncode == 0:
            return
        last = result.stderr.strip() or result.stdout.strip()
        time.sleep(0.5)
    raise DockerError(f"could not remove {image} from the nested store: {last}")


def extract_checkout_mirror(
    image: str, destination: Path, container_name: str
) -> Path:
    """Copy the bare actions/checkout mirror out of the seeded job image.

    ``docker create`` plus ``docker cp`` reads the image without starting it,
    and ``docker rm -v`` removes the scratch container together with any volume
    it might have acquired. The container is named from the run id, so even this
    short-lived resource is attributable to exactly this run.

    The extracted repository is pushed into the disposable instance, so the job
    clones the action from Forgejo instead of the internet.
    """
    created = docker("create", "--name", container_name, image).stdout.strip()
    if not created:
        raise DockerError(f"docker create {image} returned no container id")
    try:
        docker("cp", f"{created}:/opt/mirror/actions-checkout.git", str(destination))
    finally:
        docker("rm", "-v", created)
    if not (destination / "HEAD").is_file():
        raise DockerError(f"the job image has no bare checkout mirror: {destination}")
    return destination


def project_container_ids(project: str) -> list[str]:
    """Return every container this run's compose project owns, running or not."""
    output = docker(
        "ps",
        "-aq",
        "--filter",
        f"label=com.docker.compose.project={project}",
    ).stdout
    return [line.strip() for line in output.splitlines() if line.strip()]


def project_volume_names(project: str) -> list[str]:
    """Return the volume names mounted by this run's own containers.

    Anonymous volumes carry no project label, so they are attributed through
    the containers that mount them. This is the only reliable way to catch the
    dind container's anonymous ``/var/lib/docker`` volume if teardown misses it.
    """
    names: set[str] = set()
    for container in project_container_ids(project):
        mounts = docker("inspect", "--format", "{{json .Mounts}}", container).stdout
        for mount in json.loads(mounts or "[]"):
            if mount.get("Type") == "volume" and mount.get("Name"):
                names.add(str(mount["Name"]))
    return sorted(names)


def project_network_names(project: str) -> list[str]:
    """Return the networks this run's compose project owns."""
    output = docker(
        "network",
        "ls",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--format",
        "{{.Name}}",
    ).stdout
    return [line.strip() for line in output.splitlines() if line.strip()]


def surviving_resources(project: str, recorded_volumes: Sequence[str]) -> list[str]:
    """Return everything of this run's project still visible after teardown."""
    problems: list[str] = []
    containers = project_container_ids(project)
    if containers:
        problems.append(f"containers survived cleanup: {', '.join(containers)}")
    networks = project_network_names(project)
    if networks:
        problems.append(f"networks survived cleanup: {', '.join(networks)}")
    for volume in recorded_volumes:
        present = docker("volume", "inspect", volume, check=False)
        if present.returncode == 0:
            problems.append(f"volume survived cleanup: {volume}")
    return problems
