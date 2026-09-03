"""Process-death injection over the real Unix-socket tool transport."""

from __future__ import annotations

import json
import os
import signal
import tempfile
from pathlib import Path
from typing import Any

from boundaries import CrashBoundary, effect_identity, redacted_arguments

from tl_loop.client.transport import JsonObject, TransportClient

CRASH_EXIT_CODE = 97


class CrashBoundaryTransport(TransportClient):
    """Crash once at a selected real tool boundary and record its identity."""

    def __init__(
        self,
        project_root: Path,
        trace_path: Path,
        boundary: CrashBoundary,
        *,
        advance_base_after_watcher: bool = False,
        crash_owner_pid: int | None = None,
    ) -> None:
        super().__init__(project_root=project_root, timeout=10)
        self.trace_path = trace_path
        self.boundary = boundary
        self.advance_base_after_watcher = advance_base_after_watcher
        self.crash_owner_pid = crash_owner_pid
        self._watcher_calls = 0
        self._base_advanced = False
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        identity = effect_identity(arguments, tool_name)
        matched = self._matches(tool_name, arguments)
        if matched:
            self._write_record(
                {
                    "boundary": self.boundary.name,
                    "point": "before",
                    "tool_name": tool_name,
                    "identity": identity,
                    "arguments": redacted_arguments(arguments),
                }
            )
            if self.boundary.point == "before":
                self._terminate_for_boundary()
        response = super().call_tool(role, name, tool_name, arguments)
        if (
            self.advance_base_after_watcher
            and tool_name == "watcher_pr_state"
            and not self._base_advanced
        ):
            from real_server_transport import advance_remote_base

            advance_remote_base(self.project_root, 1)
            self._base_advanced = True
        if matched:
            self._write_record(
                {
                    "boundary": self.boundary.name,
                    "point": "after",
                    "tool_name": tool_name,
                    "identity": identity,
                    "arguments": redacted_arguments(arguments),
                    "success": response.get("success"),
                }
            )
            if self.boundary.point == "after":
                self._terminate_for_boundary()
        return response

    def _terminate_for_boundary(self) -> None:
        """Kill the controller even when a tool is executing in a child."""
        if self.crash_owner_pid is not None and self.crash_owner_pid != os.getpid():
            try:
                os.kill(self.crash_owner_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        os._exit(CRASH_EXIT_CODE)

    def _matches(self, tool_name: str, arguments: JsonObject) -> bool:
        if self.boundary.name == "spawn":
            # A dispatch-confirmed ledger event is only bookkeeping.  The
            # selected boundary must be the actual child-process creation
            # effect, after the child checkpoint and scheduler handoff.
            return tool_name in {"spawn_leaf", "spawn_worker"}
        if self.boundary.name == "publication":
            return tool_name == "file_pr" and not str(
                arguments.get("title", "")
            ).startswith("Aggregate ")
        if self.boundary.name == "aggregate_publication":
            return tool_name == "file_pr" and str(
                arguments.get("title", "")
            ).startswith("Aggregate ")
        if tool_name != self.boundary.tool_name:
            return False
        if self.boundary.name in {"merge_intent", "stage_release"}:
            event_type = arguments.get("event_type")
            if not isinstance(event_type, str):
                return False
            expected = (
                "tl.action_queued"
                if self.boundary.name == "merge_intent"
                else "tl.stage_completed"
            )
            return event_type == expected
        if self.boundary.name in {"review", "adoption"}:
            if not isinstance(arguments.get("pr_number"), int):
                return False
            self._watcher_calls += 1
            # Startup review validation is the first snapshot.  Adoption is
            # only eligible after that initial observation, so the two matrix
            # entries cannot both crash on the same call.
            return self._watcher_calls == (1 if self.boundary.name == "review" else 2)
        return True

    def _write_record(self, record: dict[str, Any]) -> None:
        payload = json.dumps(record, sort_keys=True) + "\n"
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.trace_path.name}.",
            suffix=".tmp",
            dir=self.trace_path.parent,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as temporary:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            with self.trace_path.open("a", encoding="utf-8") as target:
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


class RecordingTransport(TransportClient):
    """Record resumed UDS calls so post-crash redispatch is auditable."""

    def __init__(self, project_root: Path, trace_path: Path) -> None:
        super().__init__(project_root=project_root, timeout=10)
        self.trace_path = trace_path
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        response = super().call_tool(role, name, tool_name, arguments)
        self._write_record(
            {
                "tool_name": tool_name,
                "identity": effect_identity(arguments, tool_name),
                "arguments": redacted_arguments(arguments),
            }
        )
        return response

    def _write_record(self, record: dict[str, Any]) -> None:
        payload = json.dumps(record, sort_keys=True) + "\n"
        with self.trace_path.open("a", encoding="utf-8") as trace:
            trace.write(payload)
            trace.flush()
            os.fsync(trace.fileno())
