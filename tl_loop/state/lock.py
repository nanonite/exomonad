"""Advisory process lock for the TL run-state write path."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class LockTimeout(TimeoutError):
    """The run-state lock was not acquired before its deadline."""


@dataclass(frozen=True)
class LockOwner:
    """Diagnostic owner metadata stored while a lock is held."""

    pid: int
    acquired_at: float


class RunLock:
    """A non-blocking ``flock`` with timeout and stale-owner inspection."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 5.0,
        poll_interval: float = 0.01,
    ) -> None:
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("lock timeout and poll interval must be positive")
        self.path = Path(path)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._fd: int | None = None
        self.owner: LockOwner | None = None
        self.stale_owner_detected = False

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(self, _exception_type: object, _exception: object, _traceback: object) -> None:
        self.release()

    def acquire(self) -> None:
        """Acquire the lock or raise ``LockTimeout``."""
        if self._fd is not None:
            raise RuntimeError("run-state lock is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._fd = fd
                    self.owner = LockOwner(os.getpid(), time.time())
                    _write_owner(fd, self.owner)
                    return
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if owner_is_stale(self.path):
                        self.stale_owner_detected = True
                    if time.monotonic() >= deadline:
                        owner = read_owner(self.path)
                        detail = f" held by pid {owner.pid}" if owner else ""
                        raise LockTimeout(f"timed out acquiring run-state lock {self.path}{detail}") from error
                    time.sleep(min(self.poll_interval, max(0.001, deadline - time.monotonic())))
        except BaseException:
            os.close(fd)
            raise

    def release(self) -> None:
        """Release the held kernel lock without unlinking the lock file."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        self.owner = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def read_owner(path: str | Path) -> LockOwner | None:
    """Read owner metadata, returning ``None`` for absent or malformed data."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
        value: Any = json.loads(raw)
        if not isinstance(value, dict):
            return None
        pid = value.get("pid")
        acquired_at = value.get("acquired_at")
        if type(pid) is not int or pid <= 0 or not isinstance(acquired_at, (int, float)):
            return None
        return LockOwner(pid, float(acquired_at))
    except (OSError, ValueError, TypeError):
        return None


def owner_is_stale(path: str | Path) -> bool:
    """Return whether the recorded owner PID is no longer alive."""
    owner = read_owner(path)
    return owner is not None and not _pid_is_alive(owner.pid)


def _write_owner(fd: int, owner: LockOwner) -> None:
    payload = json.dumps({"pid": owner.pid, "acquired_at": owner.acquired_at}, sort_keys=True).encode("utf-8")
    os.ftruncate(fd, 0)
    written = 0
    while written < len(payload):
        written += os.write(fd, payload[written:])
    os.fsync(fd)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
