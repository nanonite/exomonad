"""The images one run needs, and the Phase 0 check that they are cached.

The host image cache is the only thing shared between runs, and a run is never
allowed to pull an image. Phase 0 therefore inspects every image before any
container starts and, when one is absent, fails with that image's name and the
exact command an operator runs once on a networked host to seed it. A missing
image is never repaired by this harness: ``docker build`` and ``docker pull``
are refused by the docker helper in :mod:`dockerops`.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence

#: The images the disposable stack and the typecheck job run from.
FORGEJO_IMAGE = "codeberg.org/forgejo/forgejo:15"
RUNNER_IMAGE = "data.forgejo.org/forgejo/runner:12"
DIND_IMAGE = "docker:28-dind"

#: The seeded job image: node, git, a working venv, just, an offline wheelhouse,
#: and a bare actions/checkout mirror. Built once on a networked host.
JOB_IMAGE = "exomonad-typecheck-ci:local"

#: Where the recipe for that image is checked in.
DOCKERFILE = "tests/e2e/lib/forgejo/typecheck-ci.Dockerfile"

#: The operator's one-time seed command. The build context must contain the
#: host's ``just`` binary because the Dockerfile does ``COPY just /usr/local/bin``.
SEED_COMMAND = (
    f"docker build -t {JOB_IMAGE} -f {DOCKERFILE} "
    "<build-context-directory-containing-just>"
)

REQUIRED_IMAGES = (FORGEJO_IMAGE, RUNNER_IMAGE, DIND_IMAGE, JOB_IMAGE)

ImageInspector = Callable[[str], bool]


class MissingImage(RuntimeError):
    """Raised when an image the run needs is absent from the host cache."""


def image_is_cached(image: str) -> bool:
    """Report whether the host cache holds ``image``, without contacting a registry."""
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def require_cached_images(
    images: Sequence[str] = REQUIRED_IMAGES,
    exists: ImageInspector = image_is_cached,
) -> None:
    """Fail before anything starts, naming every image that is not cached.

    ``exists`` is injectable so the contract test can prove the failure names
    the image and the seed command without Docker.
    """
    missing = [image for image in images if not exists(image)]
    if not missing:
        return
    raise MissingImage(
        "the host image cache is missing: "
        + ", ".join(missing)
        + ". This harness never pulls or builds an image. Seed the job image "
        "once on a networked host with: "
        + SEED_COMMAND
    )
