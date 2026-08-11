"""Structurally read-only effect capability for shadow-mode runs."""

from __future__ import annotations

from typing import Callable, NoReturn, cast

from .effects import EffectClient, TOOL_METHODS, ToolResult


class MutationBlocked(RuntimeError):
    """A shadow run attempted to invoke a mutating effect."""


READ_METHODS = frozenset(
    {
        "poll_workers",
        "watcher_pr_state",
        "session_status",
        "check_inbox",
        "memory_list",
        "continuation_brief",
        "list_agents",
        "chainlink_session_status",
        "chainlink_issue_show",
        "chainlink_issue_list",
        "chainlink_timer_status",
        "chainlink_milestone_list",
    }
)
MUTATING_METHODS = frozenset(TOOL_METHODS) - READ_METHODS


class ReadOnlyEffectClient:
    """Expose only read effects while making every known write fail closed.

    The wrapped client is private and no transport is exposed.  Known methods
    outside the read allowlist resolve to a callable that raises at invocation,
    so removing a call-site boolean cannot re-enable a write.
    """

    __slots__ = ("_client",)

    def __init__(self, client: EffectClient) -> None:
        self._client = client

    def poll_workers(self, **kwargs: object) -> ToolResult:
        return self._read("poll_workers", kwargs)

    def watcher_pr_state(self, **kwargs: object) -> ToolResult:
        return self._read("watcher_pr_state", kwargs)

    def session_status(self, **kwargs: object) -> ToolResult:
        return self._read("session_status", kwargs)

    def check_inbox(self) -> ToolResult:
        return self._read("check_inbox", {})

    def memory_list(self, **kwargs: object) -> ToolResult:
        return self._read("memory_list", kwargs)

    def continuation_brief(self) -> ToolResult:
        return self._read("continuation_brief", {})

    def list_agents(self, **kwargs: object) -> ToolResult:
        return self._read("list_agents", kwargs)

    def chainlink_session_status(self) -> ToolResult:
        return self._read("chainlink_session_status", {})

    def chainlink_issue_show(self, **kwargs: object) -> ToolResult:
        return self._read("chainlink_issue_show", kwargs)

    def chainlink_issue_list(self, **kwargs: object) -> ToolResult:
        return self._read("chainlink_issue_list", kwargs)

    def chainlink_timer_status(self, **kwargs: object) -> ToolResult:
        return self._read("chainlink_timer_status", kwargs)

    def chainlink_milestone_list(self) -> ToolResult:
        return self._read("chainlink_milestone_list", {})

    def __getattr__(self, name: str) -> Callable[..., NoReturn]:
        if name in MUTATING_METHODS:
            return cast(Callable[..., NoReturn], self._blocked(name))
        raise AttributeError(name)

    def _read(self, name: str, arguments: dict[str, object]) -> ToolResult:
        return getattr(self._client, name)(**arguments)

    @staticmethod
    def _blocked(name: str) -> Callable[..., NoReturn]:
        def reject(*_args: object, **_kwargs: object) -> NoReturn:
            raise MutationBlocked(f"shadow mode blocks mutating effect: {name}")

        return reject


__all__ = ["MUTATING_METHODS", "READ_METHODS", "MutationBlocked", "ReadOnlyEffectClient"]
