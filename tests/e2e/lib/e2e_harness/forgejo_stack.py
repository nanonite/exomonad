"""The run's own disposable Forgejo, provisioned from nothing.

Each acceptance run brings up its own Forgejo under its own unique compose
project name, from the shared template at
``tests/e2e/lib/forgejo/docker-compose.yml``, and tears it down with the same
project name. There is no shared instance, no copied database, and no
pre-existing account: the run's administrator, author, reviewer, tokens,
repository, and collaborators are all created here, and the whole stack is
removed with its project-scoped volume.

The host port is ephemeral and discovered with ``docker compose port``, so two
runs never collide and a previous run's database can never answer this run's
API calls.

A run that walks many cases can bring one up per batch rather than one for the
whole run. The batch label goes into the compose project name, so each batch's
instance is separately attributable and separately reclaimable, and
:func:`release` gives it back with the same ``down -v`` teardown the run would
have performed at its end. One container asked to carry a whole matrix is a
resource whose failure then takes down every case that has not run yet; a
container per batch bounds that, and the run's health re-check names the batch
whose instance stopped answering instead of letting every later case report the
same connection refusal as its own provisioning fault.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import string
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from . import cleanup as cl

#: The shared template every harness copies its own instance from.
COMPOSE_TEMPLATE = Path("tests") / "e2e" / "lib" / "forgejo" / "docker-compose.yml"

#: The Actions variant of the same template, for a run whose jobs execute in
#: the template's nested dockerd.
ACTIONS_COMPOSE_TEMPLATE = (
    Path("tests") / "e2e" / "lib" / "forgejo" / "docker-compose.actions.yml"
)

#: The service and container port the template publishes ephemerally.
SERVICE = "forgejo"
CONTAINER_PORT = 3000

#: Bounded wait for a freshly started runner to register itself.
RUNNER_REGISTRATION_TIMEOUT_SECONDS = 60.0

#: Bounded wait for the instance to report a passing health check.
HEALTH_TIMEOUT_SECONDS = 120.0

#: Bounded wait when re-checking an instance a run is already using.
#:
#: This is the health of a forge the run has been talking to, not a fresh one
#: starting up, so the window is far shorter than :data:`HEALTH_TIMEOUT_SECONDS`.
#: It is not zero-threshold either: an instance that is merely slow under load
#: must not cost the run a whole pass of cases, which is what an impatient probe
#: would do. The wait is paid at most once per instance, because the first
#: refusal abandons that batch, so a generous window here costs one pause rather
#: than a case at a time.
ANSWERING_TIMEOUT_SECONDS = 30.0

#: The compose-project suffix a batch label contributes to a project name. Kept
#: short so ``<prefix><run id>forgejo<batch>`` stays a name Docker accepts and a
#: name the prefix sweep still matches.
BATCH_LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,16}")

#: Bounded wait for the published port to be discoverable.
PORT_TIMEOUT_SECONDS = 60.0

#: Bounded wait for a freshly created account to become visible to the CLI.
USER_VISIBLE_TIMEOUT_SECONDS = 30.0

#: Bounded wait for a freshly created repository to become visible to the API.
REPO_VISIBLE_TIMEOUT_SECONDS = 30.0

_TOKEN = re.compile(r"\b[0-9a-f]{40}\b")


class ForgejoError(RuntimeError):
    """Raised when the run's own Forgejo cannot be provisioned as specified."""


@dataclass(frozen=True)
class Account:
    """One provisioned account and the token that authenticates it."""

    username: str
    token: str

    def basic_auth(self) -> str:
        """Return the HTTP Basic credential this account uses for git pushes.

        Forgejo accepts an access token in the password position of HTTP Basic
        for git-over-HTTP, so no interactive credential helper is involved.
        """
        return f"{self.username}:{self.token}"

    def extra_header(self) -> str:
        """Return the HTTP Basic header that carries this account's credential."""
        encoded = base64.b64encode(self.basic_auth().encode("utf-8")).decode("ascii")
        return f"Authorization: Basic {encoded}"


@dataclass(frozen=True)
class Instance:
    """The run's own Forgejo and everything provisioned on it."""

    project: str
    compose_file: Path
    base_url: str
    host: str
    admin_username: str
    author: Account
    reviewer: Account
    owner: str
    repo: str

    def api_url(self, path: str) -> str:
        return f"{self.base_url}/api/v1/{path.lstrip('/')}"

    def repository_api_url(self) -> str:
        return self.api_url(f"repos/{self.owner}/{self.repo}")

    def clone_url(self) -> str:
        return f"{self.base_url}/{self.owner}/{self.repo}.git"

    def extra_header_key(self) -> str:
        """Return the git config key the run's credential is scoped to.

        Git matches ``http.<url>.extraHeader`` by URL prefix, so the value is
        the discovered scheme and authority with a trailing slash and none of
        the path. Scoping it this way keeps the credential inside this one
        repository's config instead of a shared global one.
        """
        scheme, _, authority = self.base_url.partition("://")
        return f"http.{scheme}://{authority}/.extraheader"


# --------------------------------------------------------------------------
# Compose lifecycle
# --------------------------------------------------------------------------


def template_path(project_root: Path) -> Path:
    """Return the shared compose template, or refuse to guess at one."""
    path = project_root / COMPOSE_TEMPLATE
    if not path.is_file():
        raise ForgejoError(f"the disposable Forgejo template is missing: {path}")
    return path


def actions_template_path(project_root: Path) -> Path:
    """Return the Actions variant of the disposable template."""
    path = project_root / ACTIONS_COMPOSE_TEMPLATE
    if not path.is_file():
        raise ForgejoError(f"the disposable Actions template is missing: {path}")
    return path


def _compose(
    project: str,
    compose_file: Path,
    *arguments: str,
    check: bool = True,
    timeout: float = cl.COMPOSE_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
) -> str:
    child_env = None if env is None else {**os.environ, **env}
    result = subprocess.run(
        ["docker", "compose", "-p", project, "-f", str(compose_file), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=child_env,
    )
    if check and result.returncode:
        raise ForgejoError(
            f"docker compose {' '.join(arguments)} failed for project {project!r}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def _cli(project: str, compose_file: Path, *arguments: str) -> str:
    """Run one Forgejo CLI command inside the run's own container."""
    return _compose(
        project,
        compose_file,
        "exec",
        "-T",
        "-u",
        "git",
        SERVICE,
        "forgejo",
        *arguments,
    )


def up(project: str, compose_file: Path) -> None:
    """Bring the run's instance up and block until its health check passes."""
    _compose(project, compose_file, "up", "-d", "--wait", timeout=600.0)


def down(project: str, compose_file: Path) -> list[str]:
    """Remove the run's instance, its containers, and its project-scoped volume."""
    return cl._compose(project, compose_file, "down", "-v", "--remove-orphans")


def release(scope: cl.RunScope, instance: Instance) -> list[str]:
    """Give one instance back mid-run and stop owning it.

    A run that provisions a forge per batch releases each one when its batch
    ends, so no single container carries a whole matrix and a later batch starts
    against an instance nothing else has been pushing to. Releasing through the
    scope is what keeps the ownership honest in both directions: the project is
    removed now, forgotten now, and a refusal to remove it is recorded as the run
    owes a cleanup problem rather than being dropped on the floor.
    """
    return scope.release_compose(instance.project)


def published_host(project: str, compose_file: Path) -> str:
    """Read back the ephemeral host and port the instance was published on.

    This is why the template publishes no fixed port: the harness cannot know
    the port in advance, so it asks Docker after the fact and fails closed if
    the answer is not a single address.
    """
    deadline = time.monotonic() + PORT_TIMEOUT_SECONDS
    last = ""
    while time.monotonic() < deadline:
        output = _compose(
            project, compose_file, "port", SERVICE, str(CONTAINER_PORT), check=False
        ).strip()
        if output:
            last = output
            address = output.splitlines()[0].strip()
            if address.startswith("0.0.0.0:"):
                address = f"127.0.0.1:{address.split(':', 1)[1]}"
            if address.count(":") == 1:
                return address
        time.sleep(0.25)
    raise ForgejoError(
        f"compose project {project!r} published no discoverable address for port "
        f"{CONTAINER_PORT}: {last!r}"
    )


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------


def api(
    method: str,
    url: str,
    *,
    token: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> Any:
    """Call the instance's JSON API and return the decoded response."""
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/json")
    if token:
        request.add_header("Authorization", f"token {token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise ForgejoError(f"{method} {url} failed with {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise ForgejoError(f"{method} {url} failed: {error}") from error
    if not text.strip():
        return None
    return json.loads(text)


def _poll_health(instance: Instance, timeout: float, complaint: str) -> dict[str, Any]:
    """Poll the health endpoint until it passes, then return what it reported."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            report = api("GET", f"{instance.base_url}/api/healthz")
        except ForgejoError as error:
            last = error
        else:
            if isinstance(report, Mapping) and report.get("status") == "pass":
                return dict(report)
            last = report
        time.sleep(0.5)
    raise ForgejoError(f"{complaint}: {last!r}")


def wait_healthy(instance: Instance) -> dict[str, Any]:
    """Block until the run's instance reports a passing health check."""
    return _poll_health(
        instance,
        HEALTH_TIMEOUT_SECONDS,
        f"{instance.base_url} never became healthy",
    )


def assert_answering(instance: Instance) -> dict[str, Any]:
    """Fail, naming the instance, when an instance the run is using stops answering.

    A run that provisions one forge for many cases has to be able to say which of
    them the forge died in front of. Without this, a container killed by the host
    turns every later case into an identical connection-refused provisioning
    failure and the report reads as twenty boundary verdicts when there was one
    incident. The failure here names the compose project and the address, so the
    dead resource is the thing reported rather than each case that noticed.
    """
    return _poll_health(
        instance,
        ANSWERING_TIMEOUT_SECONDS,
        (
            f"the run's Forgejo {instance.project!r} at {instance.host} "
            f"stopped answering"
        ),
    )


# --------------------------------------------------------------------------
# Provisioning
# --------------------------------------------------------------------------


def _random_password() -> str:
    """Return a password generated per run and never committed or reused.

    The accounts are only ever driven through the container CLI or their API
    tokens, so the password is a creation-time formality. Generating it rather
    than pinning it keeps a committed constant from ever being a usable
    credential against any instance.
    """
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(32))


def _user_ids(instance: Instance) -> dict[str, int]:
    """Map username to id for every account the container's CLI knows about.

    ``admin user list`` without ``--admin`` is the only listing that includes
    the non-administrator accounts created here; ``--admin`` narrows the output
    to administrators and would hide every one of them.
    """
    ids: dict[str, int] = {}
    for line in _cli(
        instance.project, instance.compose_file, "admin", "user", "list"
    ).splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].isdigit():
            ids[fields[1]] = int(fields[0])
    return ids


def _wait_for_user(instance: Instance, username: str) -> int:
    deadline = time.monotonic() + USER_VISIBLE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        found = _user_ids(instance).get(username)
        if found is not None:
            return found
        time.sleep(0.25)
    raise ForgejoError(f"provisioned account {username!r} never became visible")


def _token_from(output: str, what: str) -> str:
    matches = _TOKEN.findall(output)
    if not matches:
        raise ForgejoError(f"no access token in the {what} output: {output.strip()!r}")
    return matches[-1]


def _create_admin(instance: Instance, username: str) -> str:
    """Create the site administrator this instance needs, if it has none.

    The instance's installer is locked and registration is disabled, so the
    container CLI is the only way in, and an administrator is what it needs.
    The account is created on the run's own instance, so it disappears with the
    instance rather than accumulating on a shared one.
    """
    if username in _user_ids(instance):
        return username
    _cli(
        instance.project,
        instance.compose_file,
        "admin",
        "user",
        "create",
        "--admin",
        "--username",
        username,
        "--password",
        _random_password(),
        "--email",
        f"{username}@example.invalid",
        "--must-change-password=false",
    )
    _wait_for_user(instance, username)
    return username


def _create_account(instance: Instance, username: str) -> Account:
    """Create one account and return it with the token creation printed.

    The account and its token come from one ``admin user create --access-token``
    call: Forgejo refuses a second token under a name that is already used, so
    the token is read from the creation output rather than generated again.
    """
    output = _cli(
        instance.project,
        instance.compose_file,
        "admin",
        "user",
        "create",
        "--username",
        username,
        "--password",
        _random_password(),
        "--email",
        f"{username}@example.invalid",
        "--must-change-password=false",
        "--access-token",
        "--access-token-name",
        f"e2e-token-{username}",
    )
    _wait_for_user(instance, username)
    return Account(username=username, token=_token_from(output, "account creation"))


def create_account(instance: Instance, username: str) -> Account:
    """Create one more account on the run's own instance and return its token.

    A harness that needs a repository under a fixed namespace (the runner's
    ``actions/checkout`` mirror, for example) creates that namespace as an
    ordinary account rather than reaching into the instance database. The
    account disappears with the instance's volume.
    """
    return _create_account(instance, username)


def compose_project_name(scope: cl.RunScope, batch: str | None = None) -> str:
    """Return the compose project name one instance of this run is brought up under.

    The name is the run's own prefix, so the prefix-driven sweep and the leak
    check both recognise it, and ``batch`` distinguishes the instances of a run
    that brings one up per batch. Without it two batches of one run would collide
    on a name, and the second would silently reuse the first's container -- the
    shared-instance outcome per-test ownership exists to prevent.
    """
    suffix = "" if batch is None else f"-{batch}"
    return f"{scope.session_prefix}forgejo{suffix}"


def provision(
    scope: cl.RunScope, project_root: Path, run_id: str, batch: str | None = None
) -> Instance:
    """Bring up and fully provision this run's own Forgejo.

    ``scope`` is what makes the instance disposable: the compose project is
    registered with it, so the run's teardown removes the instance and its
    volume even when a T-item fails partway through.

    ``batch`` names which batch of the run this instance serves, and gives each
    batch its own compose project so a run can give one back mid-run with
    :func:`release` instead of asking one container to carry every case. The
    accounts and repository are still named from ``run_id``: each batch has its
    own instance, so nothing collides with the batches before or after it.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,40}", run_id):
        raise ForgejoError(f"run id is not a safe compose project name: {run_id!r}")
    if batch is not None and not BATCH_LABEL.fullmatch(batch):
        raise ForgejoError(f"batch label is not a safe compose name suffix: {batch!r}")
    compose_file = template_path(project_root)
    project = scope.track_compose(compose_project_name(scope, batch), compose_file)
    up(project, compose_file)
    host = published_host(project, compose_file)
    instance = Instance(
        project=project,
        compose_file=compose_file,
        base_url=f"http://{host}",
        host=host,
        admin_username="",
        author=Account(username="", token=""),
        reviewer=Account(username="", token=""),
        owner="",
        repo=f"{run_id}-repo",
    )
    wait_healthy(instance)
    admin = _create_admin(instance, f"{run_id}-admin")
    author = _create_account(instance, f"{run_id}-author")
    reviewer = _create_account(instance, f"{run_id}-reviewer")
    instance = Instance(
        project=project,
        compose_file=compose_file,
        base_url=instance.base_url,
        host=host,
        admin_username=admin,
        author=author,
        reviewer=reviewer,
        owner=author.username,
        repo=instance.repo,
    )
    create_repository(instance, instance.repo)
    _add_collaborator(instance)
    return instance


def _runner_token(output: str) -> str:
    """Return the registration token the Forgejo CLI printed, or fail closed."""
    matches = re.findall(r"[A-Za-z0-9_-]{20,}", output)
    if not matches:
        raise ForgejoError(
            f"forgejo actions generate-runner-token printed no token: {output.strip()!r}"
        )
    return matches[-1]


def wait_runner_registered(
    project: str,
    compose_file: Path,
    timeout: float = RUNNER_REGISTRATION_TIMEOUT_SECONDS,
) -> None:
    """Block until the template's runner has registered with this instance.

    ``docker compose up --wait`` only waits for the container to be running;
    registration happens a moment later inside it. A job pushed before that
    window would sit in the queue until the runner appears, so the harness waits
    for the runner's own success line instead of guessing at a delay.
    """
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = _compose(project, compose_file, "logs", "--no-color", "runner")
        if "Runner registered successfully." in last or "declared successfully" in last:
            return
        time.sleep(0.25)
    raise ForgejoError(
        f"the runner for {project!r} did not register within {timeout:.0f}s: "
        f"{last[-2000:].strip()}"
    )


def provision_actions(
    scope: cl.RunScope,
    project_root: Path,
    run_id: str,
    job_image: str,
    batch: str | None = None,
) -> Instance:
    """Bring up the Actions template and provision this run's own instance.

    This is :func:`provision` for a run that executes jobs in the template's
    nested dockerd. The difference is the registration window: the runner must
    be registered before a pushed workflow can be picked up, and the runner
    needs the token the Forgejo container CLI prints. Forgejo and dind are
    therefore started first, the token is generated, and only then is the
    runner started with the label that maps ``ubuntu-latest`` to the caller's
    job image.

    The nested image store is deliberately left empty here. Loading the job
    image is the caller's step, because only the caller knows which image the
    workflow's ``runs-on`` label resolves to.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,40}", run_id):
        raise ForgejoError(f"run id is not a safe compose project name: {run_id!r}")
    if batch is not None and not BATCH_LABEL.fullmatch(batch):
        raise ForgejoError(f"batch label is not a safe compose name suffix: {batch!r}")
    compose_file = actions_template_path(project_root)
    project = scope.track_compose(compose_project_name(scope, batch), compose_file)
    _compose(project, compose_file, "up", "-d", "--wait", "forgejo", "dind", timeout=600.0)
    host = published_host(project, compose_file)
    instance = Instance(
        project=project,
        compose_file=compose_file,
        base_url=f"http://{host}",
        host=host,
        admin_username="",
        author=Account(username="", token=""),
        reviewer=Account(username="", token=""),
        owner="",
        repo=f"{run_id}-repo",
    )
    wait_healthy(instance)
    admin = _create_admin(instance, f"{run_id}-admin")
    author = _create_account(instance, f"{run_id}-author")
    reviewer = _create_account(instance, f"{run_id}-reviewer")
    instance = Instance(
        project=project,
        compose_file=compose_file,
        base_url=instance.base_url,
        host=host,
        admin_username=admin,
        author=author,
        reviewer=reviewer,
        owner=author.username,
        repo=instance.repo,
    )
    token = _runner_token(
        _cli(project, compose_file, "actions", "generate-runner-token")
    )
    _compose(
        project,
        compose_file,
        "up",
        "-d",
        "--wait",
        "runner",
        timeout=600.0,
        env={
            "FORGEJO_RUNNER_TOKEN": token,
            "FORGEJO_RUNNER_NAME": f"{project}-runner",
            "FORGEJO_RUNNER_LABELS": f"ubuntu-latest:docker://{job_image}",
        },
    )
    wait_runner_registered(project, compose_file)
    create_repository(instance, instance.repo)
    _add_collaborator(instance)
    return instance


def create_repository(instance: Instance, name: str) -> Instance:
    """Create one more repository on this run's own instance and select it.

    A run that walks many cases against one forge needs each case to start from
    an empty history. A case publishes branches such as ``main.sub-a`` and
    ``aggregate/<case>/<name>``, and a repository that already carries them
    rejects the push as a non-fast-forward, so every case after the first would
    be testing the previous case's leftovers instead of its own. One repository
    per case is far cheaper than one Forgejo per case and leaves no shared
    remote at all.

    The instance is the one the caller was given -- a run's current batch
    instance -- so a repository name only has to be unique inside it, and the
    ``down -v`` that releases that batch takes every repository on it.

    The result is the same instance with the new repository selected, so
    ``repository_api_url()`` and ``clone_url()`` address it while the accounts,
    the compose project, and the base URL stay the ones this run provisioned.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", name):
        raise ForgejoError(f"repository name is not one Forgejo will accept: {name!r}")
    scoped = replace(instance, repo=name)
    _create_repository(scoped)
    return scoped


def create_repository_for(
    instance: Instance, account: Account, name: str, auto_init: bool = True
) -> Instance:
    """Create one repository under ``account``'s namespace and select it.

    The runner clones ``actions/checkout`` from the instance itself, so a
    harness that mirrors the action needs a repository in that exact namespace.
    The account is created through the same CLI path as every other account, so
    the repository belongs to the disposable instance and disappears with it.

    ``auto_init=False`` leaves the repository empty, which is what a mirror
    push needs: Forgejo refuses to delete the default branch that auto-init
    creates, and a mirror push deletes refs it does not carry.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", name):
        raise ForgejoError(f"repository name is not one Forgejo will accept: {name!r}")
    scoped = replace(instance, owner=account.username, repo=name)
    _create_repository(scoped, token=account.token, auto_init=auto_init)
    return scoped


def _create_repository(
    instance: Instance, token: str | None = None, auto_init: bool = True
) -> None:
    """Create the run's own repository, and wait until the API can read it."""
    credential = instance.author.token if token is None else token
    deadline = time.monotonic() + REPO_VISIBLE_TIMEOUT_SECONDS
    last: Any = None
    while time.monotonic() < deadline:
        try:
            created = api(
                "POST",
                f"{instance.base_url}/api/v1/user/repos",
                token=credential,
                payload={
                    "name": instance.repo,
                    "auto_init": auto_init,
                    "default_branch": "main",
                    "private": False,
                    "description": f"disposable acceptance repository for {instance.project}",
                },
            )
        except ForgejoError as error:
            last = error
            time.sleep(0.5)
            continue
        if isinstance(created, Mapping) and created.get("full_name") == (
            f"{instance.owner}/{instance.repo}"
        ):
            return
        last = created
        time.sleep(0.5)
    raise ForgejoError(f"the run's repository never became visible: {last!r}")


def _add_collaborator(instance: Instance) -> None:
    """Give the reviewer write access, and prove the grant took effect."""
    api(
        "PUT",
        f"{instance.repository_api_url()}/collaborators/{instance.reviewer.username}",
        token=instance.author.token,
        payload={"permission": "write"},
    )
    listed = api(
        "GET",
        f"{instance.repository_api_url()}/collaborators",
        token=instance.author.token,
    )
    if not isinstance(listed, list) or instance.reviewer.username not in {
        entry.get("login") for entry in listed if isinstance(entry, Mapping)
    }:
        raise ForgejoError(
            f"the reviewer was not added as a collaborator: {listed!r}"
        )


__all__ = [
    "ACTIONS_COMPOSE_TEMPLATE",
    "ANSWERING_TIMEOUT_SECONDS",
    "COMPOSE_TEMPLATE",
    "RUNNER_REGISTRATION_TIMEOUT_SECONDS",
    "Account",
    "ForgejoError",
    "Instance",
    "actions_template_path",
    "api",
    "assert_answering",
    "compose_project_name",
    "create_account",
    "create_repository",
    "create_repository_for",
    "down",
    "provision",
    "provision_actions",
    "published_host",
    "release",
    "template_path",
    "up",
    "wait_healthy",
    "wait_runner_registered",
]
