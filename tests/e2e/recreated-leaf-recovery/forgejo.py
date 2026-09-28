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
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import string
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import cleanup as cl

#: The shared template every harness copies its own instance from.
COMPOSE_TEMPLATE = Path("tests") / "e2e" / "lib" / "forgejo" / "docker-compose.yml"

#: The service and container port the template publishes ephemerally.
SERVICE = "forgejo"
CONTAINER_PORT = 3000

#: Bounded wait for the instance to report a passing health check.
HEALTH_TIMEOUT_SECONDS = 120.0

#: Bounded wait for the published port to be discoverable.
PORT_TIMEOUT_SECONDS = 60.0

#: Bounded wait for a freshly created account to become visible to the CLI.
USER_VISIBLE_TIMEOUT_SECONDS = 30.0

#: Bounded wait for a freshly created repository to become visible to the API.
REPO_VISIBLE_TIMEOUT_SECONDS = 30.0

_TOKEN = re.compile(r"\b[0-9a-f]{40}\b")

#: The instance's own listener, used to build a repository URL. The discovered
#: host port is what the harness actually talks to.


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


def _compose(
    project: str,
    compose_file: Path,
    *arguments: str,
    check: bool = True,
    timeout: float = cl.COMPOSE_TIMEOUT_SECONDS,
) -> str:
    result = subprocess.run(
        ["docker", "compose", "-p", project, "-f", str(compose_file), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
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


def wait_healthy(instance: Instance) -> dict[str, Any]:
    """Block until the run's instance reports a passing health check."""
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    last: Any = None
    while time.monotonic() < deadline:
        try:
            report = api("GET", f"{instance.base_url}/api/healthz")
        except ForgejoError as error:
            last = error
            time.sleep(0.5)
            continue
        if isinstance(report, Mapping) and report.get("status") == "pass":
            return dict(report)
        last = report
        time.sleep(0.5)
    raise ForgejoError(f"{instance.base_url} never became healthy: {last!r}")


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
    for line in _cli(instance.project, instance.compose_file, "admin", "user", "list").splitlines():
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
        f"e2e-1111-{username}",
    )
    _wait_for_user(instance, username)
    return Account(username=username, token=_token_from(output, "account creation"))


def provision(scope: cl.RunScope, project_root: Path, run_id: str) -> Instance:
    """Bring up and fully provision this run's own Forgejo.

    ``scope`` is what makes the instance disposable: the compose project is
    registered with it, so the run's teardown removes the instance and its
    volume even when a T-item fails partway through.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,40}", run_id):
        raise ForgejoError(f"run id is not a safe compose project name: {run_id!r}")
    compose_file = template_path(project_root)
    project = scope.track_compose(f"{scope.session_prefix}forgejo", compose_file)
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
    _create_repository(instance)
    _add_collaborator(instance)
    return instance


def _create_repository(instance: Instance) -> None:
    """Create the run's own repository, and wait until the API can read it."""
    deadline = time.monotonic() + REPO_VISIBLE_TIMEOUT_SECONDS
    last: Any = None
    while time.monotonic() < deadline:
        try:
            created = api(
                "POST",
                f"{instance.base_url}/api/v1/user/repos",
                token=instance.author.token,
                payload={
                    "name": instance.repo,
                    "auto_init": True,
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
