"""Per-run Codex home for the real-server E2E acceptances.

ExoMonad seeds Codex hook trust by rewriting the *user* Codex config that
``codex_config::codex_user_config_path()`` resolves: ``$CODEX_HOME/config.toml``
when the variable is set, ``$HOME/.codex/config.toml`` when it is not. An
acceptance that leaves ``CODEX_HOME`` unset therefore edits the operator's real
config while it runs, and the second acceptance to start then sees entries the
first one left behind. Copying the host config aside and restoring it afterwards
is worse: ExoMonad rewrites the file in place, and a restore that never runs
because the run died leaves the operator's config corrupted.

The contract is therefore the one implemented here. Every run gets its own
Codex home beneath its own directory, and every environment handed to an
ExoMonad process carries it. The host config is never read, copied, or
restored; :func:`host_config_digest` is a *sentinel*, read-only and recorded so
teardown can prove the run left the operator's file byte-for-byte alone.

A harness whose ``codex`` binary is a fixture never needs credentials, so
:func:`isolate` copies nothing by default. Only an acceptance that starts a
*real* Codex process may pass ``copy_auth=True``; the artifact list is
deliberately narrow and deliberately excludes ``config.toml``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Mapping, MutableMapping
from pathlib import Path

#: Directory name of the per-run Codex home inside the run's own directory.
CODEX_HOME_DIRNAME = "codex-home"

#: The only host artifacts a *real* ``codex`` process needs in order to
#: authenticate. Deliberately narrow, and deliberately not ``config.toml``:
#: rewriting that file is exactly what this module exists to contain.
AUTH_ARTIFACTS: tuple[str, ...] = ("auth.json", "installation_id")

#: Reported for a host config that does not exist, so an absent file is
#: distinguishable from an empty one.
ABSENT_DIGEST = "absent"

#: How much of a host config is read to compute its sentinel digest. 64 KiB is
#: far past any real ``config.toml`` and keeps the check bounded.
_DIGEST_CHUNK_BYTES = 65536

#: Sentinel digests recorded by :func:`isolate`, keyed by the run's own
#: directory. Teardown looks the run up by that directory, which is the only
#: handle it is guaranteed to still have, and compares the host config against
#: what it was before the ExoMonad process ever started.
_SENTINELS: dict[Path, str] = {}


class CodexHomeError(RuntimeError):
    """Raised when a run's Codex home is not, or cannot be made, run-scoped."""


def host_home(home: str | Path | None = None) -> Path:
    """Return the host Codex home: the fallback ``codex_user_config_path`` uses."""
    base = Path(home) if home is not None else Path.home()
    return base / ".codex"


def host_config(home: str | Path | None = None) -> Path:
    """Return the host Codex user config, the file an unisolated run rewrites."""
    return host_home(home) / "config.toml"


def file_digest(path: str | Path) -> str:
    """Return a content digest of ``path``, or :data:`ABSENT_DIGEST` if missing."""
    target = Path(path)
    if not target.exists():
        return ABSENT_DIGEST
    if target.is_dir():
        raise CodexHomeError(f"expected a file at {target} but found a directory")
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_DIGEST_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def host_config_digest(home: str | Path | None = None) -> str:
    """Return the sentinel digest of the host Codex user config.

    Read-only, and recorded rather than acted on: nothing here ever writes,
    moves, or restores that file.
    """
    return file_digest(host_config(home))


def isolate(
    root: str | Path,
    environment: MutableMapping[str, str],
    *,
    home: str | Path | None = None,
    copy_auth: bool = False,
) -> Path:
    """Create this run's Codex home under ``root`` and point ``environment`` at it.

    ``environment`` is the mapping that will be handed to ``exomonad init`` or
    ``exomonad serve``, and is mutated in place because every child of that
    process -- including the tmux panes it spawns agents into -- must resolve
    the same Codex home. Call this before the ExoMonad process starts; a
    ``CODEX_HOME`` set afterwards never reaches a process already running.

    ``copy_auth=True`` is for an acceptance that starts a real ``codex``
    process, and raises when the host carries none of the documented
    artifacts, because a live Codex that cannot authenticate fails later and
    far from the cause.
    """
    codex_home = Path(root) / CODEX_HOME_DIRNAME
    codex_home.mkdir(parents=True, exist_ok=True)
    environment["CODEX_HOME"] = str(codex_home)
    _SENTINELS[Path(root).resolve()] = host_config_digest(home)
    if copy_auth:
        copy_auth_artifacts(codex_home, home=home)
    return codex_home


def assert_untouched(root: str | Path, *, home: str | Path | None = None) -> None:
    """Assert the host Codex config is byte-for-byte what it was at ``isolate``.

    Teardown calls this while the run's own directory still exists, so the
    sentinel is checked before anything is deleted. Raises
    :class:`CodexHomeError` if the run is unknown or the file changed.
    """
    run_root = Path(root).resolve()
    recorded = _SENTINELS.get(run_root)
    if recorded is None:
        raise CodexHomeError(
            f"no Codex home sentinel recorded for {run_root}; the run never called isolate()"
        )
    current = host_config_digest(home)
    if current != recorded:
        raise CodexHomeError(
            f"host Codex config {host_config(home)} changed during the run "
            f"({recorded} -> {current})"
        )


def copy_auth_artifacts(
    codex_home: str | Path, *, home: str | Path | None = None
) -> tuple[Path, ...]:
    """Copy the documented authentication artifacts into ``codex_home``.

    Returns the artifacts actually copied. Raises when none were found, so an
    acceptance that needs a live Codex session cannot start on a host that
    cannot authenticate one.
    """
    source = host_home(home)
    destination = Path(codex_home)
    copied: list[Path] = []
    for artifact in AUTH_ARTIFACTS:
        origin = source / artifact
        if origin.is_file():
            target = destination / artifact
            shutil.copy2(origin, target)
            copied.append(target)
    if not copied:
        raise CodexHomeError(
            f"no Codex auth artifact in {source}; a live Codex E2E cannot "
            f"authenticate. Looked for: {', '.join(AUTH_ARTIFACTS)}"
        )
    return tuple(copied)


def assert_run_scoped(
    root: str | Path,
    environment: Mapping[str, str],
    *,
    home: str | Path | None = None,
) -> None:
    """Assert this run's ``CODEX_HOME`` is beneath ``root``.

    Raises :class:`CodexHomeError` otherwise. A shared home is a different kind
    of leak from an absent one: two runs pointing at one Codex home race on the
    trust lock, and one run's teardown removes the other's state. The host
    sentinel is a separate check, :func:`assert_untouched`.
    """
    run_root = Path(root).resolve()
    codex_home = environment.get("CODEX_HOME")
    if not codex_home:
        raise CodexHomeError(
            "CODEX_HOME is unset; this run would rewrite the host Codex config"
        )
    resolved = Path(codex_home).resolve()
    if resolved != run_root and run_root not in resolved.parents:
        raise CodexHomeError(
            f"CODEX_HOME ({resolved}) is not beneath the run directory ({run_root})"
        )
