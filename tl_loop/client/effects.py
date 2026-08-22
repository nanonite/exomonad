"""Typed effect methods for the tools owned by the TL controller."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypeAlias, cast

from .transport import DecodeError, JsonObject, JsonValue

StringList: TypeAlias = Sequence[str]


class EffectTransport(Protocol):
    """The transport capability required by the effect client."""

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        """Call a named server-side tool."""


@dataclass(frozen=True)
class CompletedTask:
    """A task and its verification command for ``notify_parent``."""

    what: str
    how: str

    def to_json(self) -> JsonObject:
        """Encode the completed task pair."""
        return {"what": self.what, "how": self.how}


@dataclass(frozen=True)
class ToolResult:
    """Typed envelope returned by every effect method."""

    raw: JsonObject
    success: bool | None
    result: JsonValue | None
    error: str | None

    @classmethod
    def from_raw(cls, raw: JsonObject) -> ToolResult:
        """Decode the stable server envelope without interpreting its result."""
        success = raw.get("success")
        if success is not None and not isinstance(success, bool):
            raise DecodeError(f"Tool result success is not a boolean: {raw!r}")
        error = raw.get("error")
        if error is not None and not isinstance(error, str):
            raise DecodeError(f"Tool result error is not a string: {raw!r}")
        return cls(raw=raw, success=success, result=raw.get("result"), error=error)


TOOL_METHODS: tuple[str, ...] = (
    "spawn_leaf",
    "spawn_worker",
    "spawn_reviewer",
    "cleanup_reviewer_leaf",
    "close_reviewer_window",
    "restart_review",
    "replace_close_pr",
    "resume_pr",
    "resolve_live_pr_for_slice",
    "watcher_pr_state",
    "close_worker_pane",
    "spawn_codex",
    "session_status",
    "poll_workers",
    "check_inbox",
    "emit_controller_event",
    "memory_append",
    "memory_list",
    "continuation_brief",
    "list_agents",
    "file_pr",
    "merge_pr",
    "notify_parent",
    "send_tmux_message",
    "send_mailbox_message",
    "chainlink_issue_create",
    "chainlink_session_start",
    "chainlink_session_status",
    "chainlink_issue_show",
    "chainlink_issue_comment",
    "chainlink_subissue_create",
    "chainlink_session_work",
    "chainlink_session_end",
    "chainlink_issue_close",
    "close_issue_and_cleanup",
    "cleanup_orphan",
    "cleanup_leaf",
    "cleanup",
    "chainlink_timer_start",
    "chainlink_timer_stop",
    "chainlink_timer_status",
    "chainlink_issue_list",
    "chainlink_issue_update",
    "chainlink_issue_block",
    "chainlink_issue_relate",
    "chainlink_issue_cascade",
    "chainlink_milestone_create",
    "chainlink_milestone_list",
)


class EffectClient:
    """One typed method per tool exposed by the TL role."""

    def __init__(
        self,
        transport: EffectTransport,
        *,
        role: str = "tl",
        name: str = "root",
    ) -> None:
        self.transport = transport
        self.role = role
        self.name = name

    def _call(self, tool_name: str, arguments: JsonObject) -> ToolResult:
        response = self.transport.call_tool(self.role, self.name, tool_name, arguments)
        return ToolResult.from_raw(response)

    def emit_controller_event(self, *, event_type: str, payload: JsonObject) -> ToolResult:
        """Emit bounded controller dimensions through the Rust ledger writer."""
        return self._call(
            "emit_controller_event",
            {"event_type": event_type, "payload": payload},
        )

    def spawn_leaf(
        self,
        *,
        name: str,
        task: str,
        intent_id: str | None = None,
        agent_type: str | None = None,
        model: str | None = None,
        boundary: StringList | None = None,
        context: str | None = None,
        read_first: StringList | None = None,
        steps: StringList | None = None,
        verify: StringList | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {"name": name, "task": task}
        _put(arguments, "intent_id", intent_id)
        _put(arguments, "agent_type", agent_type)
        _put(arguments, "model", model)
        _put_list(arguments, "boundary", boundary)
        _put(arguments, "context", context)
        _put_list(arguments, "read_first", read_first)
        _put_list(arguments, "steps", steps)
        _put_list(arguments, "verify", verify)
        return self._call("spawn_leaf", arguments)

    def spawn_worker(
        self,
        *,
        name: str,
        task: str,
        intent_id: str | None = None,
        agent_type: str | None = None,
        model: str | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {"name": name, "task": task}
        _put(arguments, "intent_id", intent_id)
        _put(arguments, "agent_type", agent_type)
        _put(arguments, "model", model)
        return self._call("spawn_worker", arguments)

    def spawn_reviewer(
        self,
        *,
        pr_number: int,
        head_sha: str,
        acceptance_criteria: StringList,
        force: bool,
    ) -> ToolResult:
        arguments: JsonObject = {"pr_number": pr_number, "head_sha": head_sha, "force": force}
        _put_list(arguments, "acceptance_criteria", acceptance_criteria)
        return self._call("spawn_reviewer", arguments)

    def cleanup_reviewer_leaf(self, *, pr_number: int) -> ToolResult:
        return self._call("cleanup_reviewer_leaf", {"pr_number": pr_number})

    def close_reviewer_window(self, *, pr_number: int) -> ToolResult:
        return self._call("close_reviewer_window", {"pr_number": pr_number})

    def restart_review(self, *, pr_number: int) -> ToolResult:
        return self._call("restart_review", {"pr_number": pr_number})

    def replace_close_pr(
        self,
        *,
        chainlink_issue_id: int,
        closed_pr_number: int,
        old_leaf_name: str,
        new_leaf_name: str,
        replacement_task: str,
        human_approved: bool,
        agent_type: str | None = None,
        operator_context: str | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {
            "chainlink_issue_id": chainlink_issue_id,
            "closed_pr_number": closed_pr_number,
            "old_leaf_name": old_leaf_name,
            "new_leaf_name": new_leaf_name,
            "replacement_task": replacement_task,
            "human_approved": human_approved,
        }
        _put(arguments, "agent_type", agent_type)
        _put(arguments, "operator_context", operator_context)
        return self._call("replace_close_pr", arguments)

    def resume_pr(
        self,
        *,
        pr_number: int,
        task: str,
        boundary: StringList | None = None,
        context: str | None = None,
        done_criteria: StringList | None = None,
        read_first: StringList | None = None,
        steps: StringList | None = None,
        verify: StringList | None = None,
        model: str | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {"pr_number": pr_number, "task": task}
        _put_list(arguments, "boundary", boundary)
        _put(arguments, "context", context)
        _put_list(arguments, "done_criteria", done_criteria)
        _put_list(arguments, "read_first", read_first)
        _put_list(arguments, "steps", steps)
        _put_list(arguments, "verify", verify)
        _put(arguments, "model", model)
        return self._call("resume_pr", arguments)

    def watcher_pr_state(
        self, *, pr_number: int
    ) -> ToolResult:
        return self._call("watcher_pr_state", {"pr_number": pr_number})

    def resolve_live_pr_for_slice(self, *, slice_id: str) -> ToolResult:
        return self._call("resolve_live_pr_for_slice", {"slice_id": slice_id})

    def close_worker_pane(self, *, pane_id: str) -> ToolResult:
        return self._call("close_worker_pane", {"pane_id": pane_id})

    def cleanup(self, *, issue: str, force: bool = False, subrepo: str = "") -> ToolResult:
        return self._call(
            "cleanup",
            {"issue": issue, "force": force, "subrepo": subrepo},
        )

    def spawn_codex(
        self,
        *,
        task: str,
        branch_name: str,
        agent_type: str | None = None,
        allowed_dirs: StringList | None = None,
        allowed_tools: StringList | None = None,
        disallowed_tools: StringList | None = None,
        permission_mode: str | None = None,
        standalone_repo: bool | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {"task": task, "branch_name": branch_name}
        _put(arguments, "agent_type", agent_type)
        _put_list(arguments, "allowed_dirs", allowed_dirs)
        _put_list(arguments, "allowed_tools", allowed_tools)
        _put_list(arguments, "disallowed_tools", disallowed_tools)
        _put(arguments, "permission_mode", permission_mode)
        _put(arguments, "standalone_repo", standalone_repo)
        return self._call("spawn_codex", arguments)

    def session_status(self, *, include_dead: bool) -> ToolResult:
        return self._call("session_status", {"include_dead": include_dead})

    def poll_workers(
        self,
        *,
        include_dead: bool,
        agents: StringList | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {"include_dead": include_dead}
        _put_list(arguments, "agents", agents)
        return self._call("poll_workers", arguments)

    def check_inbox(self) -> ToolResult:
        return self._call("check_inbox", {})

    def memory_append(
        self,
        *,
        append_args_kind: str,
        append_args_summary: str,
        append_args_detail: str | None = None,
        append_args_importance: int | None = None,
        append_args_issue_id: int | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {
            "append_args_kind": append_args_kind,
            "append_args_summary": append_args_summary,
        }
        _put(arguments, "append_args_detail", append_args_detail)
        _put(arguments, "append_args_importance", append_args_importance)
        _put(arguments, "append_args_issue_id", append_args_issue_id)
        return self._call("memory_append", arguments)

    def memory_list(
        self,
        *,
        list_args_issue_id: int | None = None,
        list_args_kind: str | None = None,
        list_args_limit: int | None = None,
        list_args_min_importance: int | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {}
        _put(arguments, "list_args_issue_id", list_args_issue_id)
        _put(arguments, "list_args_kind", list_args_kind)
        _put(arguments, "list_args_limit", list_args_limit)
        _put(arguments, "list_args_min_importance", list_args_min_importance)
        return self._call("memory_list", arguments)

    def continuation_brief(self) -> ToolResult:
        return self._call("continuation_brief", {})

    def list_agents(self, *, filter_type: str | None = None) -> ToolResult:
        arguments: JsonObject = {}
        _put(arguments, "filter_type", filter_type)
        return self._call("list_agents", arguments)

    def file_pr(
        self,
        *,
        title: str,
        body: str,
        base_branch: str | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {"title": title, "body": body}
        _put(arguments, "base_branch", base_branch)
        return self._call("file_pr", arguments)

    def merge_pr(
        self,
        *,
        pr_number: int,
        chainlink_issue_id: int | None = None,
        strategy: str | None = None,
        working_dir: str | None = None,
        expected_base_sha: str | None = None,
        expected_head_sha: str | None = None,
        expected_patch_digest: str | None = None,
        expected_merge_tree_sha: str | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {"pr_number": pr_number}
        _put(arguments, "chainlink_issue_id", chainlink_issue_id)
        _put(arguments, "strategy", strategy)
        _put(arguments, "working_dir", working_dir)
        _put(arguments, "expected_base_sha", expected_base_sha)
        _put(arguments, "expected_head_sha", expected_head_sha)
        _put(arguments, "expected_patch_digest", expected_patch_digest)
        _put(arguments, "expected_merge_tree_sha", expected_merge_tree_sha)
        return self._call("merge_pr", arguments)

    def notify_parent(
        self,
        *,
        status: str,
        message: str,
        pr_number: int | None = None,
        tasks_completed: Sequence[CompletedTask | JsonObject] | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {"status": status, "message": message}
        _put(arguments, "pr_number", pr_number)
        if tasks_completed is not None:
            arguments["tasks_completed"] = [_structured(task) for task in tasks_completed]
        return self._call("notify_parent", arguments)

    def send_tmux_message(
        self,
        *,
        recipient: str,
        content: str,
        summary: str | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {"recipient": recipient, "content": content}
        _put(arguments, "summary", summary)
        return self._call("send_tmux_message", arguments)

    def send_mailbox_message(
        self,
        *,
        recipient: str,
        content: str,
        summary: str | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {"recipient": recipient, "content": content}
        _put(arguments, "summary", summary)
        return self._call("send_mailbox_message", arguments)

    def chainlink_issue_create(
        self,
        *,
        title: str,
        description: str | None = None,
        labels: StringList | None = None,
        priority: str | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {"title": title}
        _put(arguments, "description", description)
        _put_list(arguments, "labels", labels)
        _put(arguments, "priority", priority)
        return self._call("chainlink_issue_create", arguments)

    def chainlink_session_start(self) -> ToolResult:
        return self._call("chainlink_session_start", {})

    def chainlink_session_status(self) -> ToolResult:
        return self._call("chainlink_session_status", {})

    def chainlink_issue_show(self, *, issue_id: int) -> ToolResult:
        return self._call("chainlink_issue_show", {"issue_id": issue_id})

    def chainlink_issue_comment(self, *, issue_id: int, message: str) -> ToolResult:
        return self._call("chainlink_issue_comment", {"issue_id": issue_id, "message": message})

    def chainlink_subissue_create(
        self,
        *,
        parent_id: int,
        title: str,
        labels: StringList | None = None,
        priority: str | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {"parent_id": parent_id, "title": title}
        _put_list(arguments, "labels", labels)
        _put(arguments, "priority", priority)
        return self._call("chainlink_subissue_create", arguments)

    def chainlink_session_work(self, *, issue_id: int) -> ToolResult:
        return self._call("chainlink_session_work", {"issue_id": issue_id})

    def chainlink_session_end(self, *, notes: str | None = None) -> ToolResult:
        arguments: JsonObject = {}
        _put(arguments, "notes", notes)
        return self._call("chainlink_session_end", arguments)

    def chainlink_issue_close(
        self,
        *,
        issue_id: int,
        force: bool,
        summary: str | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {"issue_id": issue_id, "force": force}
        _put(arguments, "summary", summary)
        return self._call("chainlink_issue_close", arguments)

    def close_issue_and_cleanup(self, *, issue_id: int, leaf_name: str) -> ToolResult:
        return self._call("close_issue_and_cleanup", {"issue_id": issue_id, "leaf_name": leaf_name})

    def cleanup_orphan(self, *, name: str, dry_run: bool) -> ToolResult:
        return self._call("cleanup_orphan", {"name": name, "dry_run": dry_run})

    def cleanup_leaf(
        self,
        *,
        dry_run: bool,
        sweep: bool,
        name: str | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {"dry_run": dry_run, "sweep": sweep}
        _put(arguments, "name", name)
        return self._call("cleanup_leaf", arguments)

    def chainlink_timer_start(self, *, issue_id: int) -> ToolResult:
        return self._call("chainlink_timer_start", {"issue_id": issue_id})

    def chainlink_timer_stop(self, *, issue_id: int) -> ToolResult:
        return self._call("chainlink_timer_stop", {"issue_id": issue_id})

    def chainlink_timer_status(self, *, issue_id: int | None = None) -> ToolResult:
        arguments: JsonObject = {}
        _put(arguments, "issue_id", issue_id)
        return self._call("chainlink_timer_status", arguments)

    def chainlink_issue_list(
        self,
        *,
        labels: StringList | None = None,
        milestone: str | None = None,
        priority: str | None = None,
        status: str | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {}
        _put_list(arguments, "labels", labels)
        _put(arguments, "milestone", milestone)
        _put(arguments, "priority", priority)
        _put(arguments, "status", status)
        return self._call("chainlink_issue_list", arguments)

    def chainlink_issue_update(
        self,
        *,
        issue_id: int,
        labels: StringList | None = None,
        milestone: str | None = None,
        priority: str | None = None,
        status: str | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {"issue_id": issue_id}
        _put_list(arguments, "labels", labels)
        _put(arguments, "milestone", milestone)
        _put(arguments, "priority", priority)
        _put(arguments, "status", status)
        return self._call("chainlink_issue_update", arguments)

    def chainlink_issue_block(self, *, child_id: int, blocker_id: int) -> ToolResult:
        return self._call("chainlink_issue_block", {"child_id": child_id, "blocker_id": blocker_id})

    def chainlink_issue_relate(self, *, issue1: int, issue2: int) -> ToolResult:
        return self._call("chainlink_issue_relate", {"issue1": issue1, "issue2": issue2})

    def chainlink_issue_cascade(self, *, issue_id: int) -> ToolResult:
        return self._call("chainlink_issue_cascade", {"issue_id": issue_id})

    def chainlink_milestone_create(
        self,
        *,
        title: str,
        description: str | None = None,
    ) -> ToolResult:
        arguments: JsonObject = {"title": title}
        _put(arguments, "description", description)
        return self._call("chainlink_milestone_create", arguments)

    def chainlink_milestone_list(self) -> ToolResult:
        return self._call("chainlink_milestone_list", {})


def _put(arguments: JsonObject, key: str, value: JsonValue | None) -> None:
    if value is not None:
        arguments[key] = value


def _put_list(arguments: JsonObject, key: str, value: StringList | None) -> None:
    if value is not None:
        arguments[key] = list(value)


def _structured(value: CompletedTask | JsonObject) -> JsonObject:
    if isinstance(value, CompletedTask):
        return value.to_json()
    return cast(JsonObject, dict(value))
