"""Contract tests for the typed TL effect surface."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from tl_loop.client.effects import (
    TOOL_METHODS,
    ChildSpec,
    CompletedTask,
    EffectClient,
    ToolResult,
)
from tl_loop.client.transport import JsonObject, JsonValue

FIXTURE = Path(__file__).parent / "fixtures" / "tool_schemas.json"


@dataclass
class RecordingTransport:
    """Transport double that records calls without performing I/O."""

    calls: list[tuple[str, JsonObject]] = field(default_factory=list)

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        del role, name
        self.calls.append((tool_name, arguments))
        return {"success": True, "result": None}


def test_live_tool_snapshot_has_one_client_method() -> None:
    """The live TL snapshot and typed client must evolve together."""
    schemas = _load_schemas()
    assert set(schemas) == set(TOOL_METHODS)
    for tool_name in schemas:
        assert callable(getattr(EffectClient, tool_name, None)), tool_name


def test_generated_payloads_conform_to_live_tool_snapshot() -> None:
    """Every typed method must emit a payload accepted by its live schema."""
    transport = RecordingTransport()
    client = EffectClient(transport)
    _invoke_sample_effects(client)

    schemas = _load_schemas()
    called_names = {name for name, _ in transport.calls}
    assert called_names == set(schemas)
    assert len(transport.calls) == len(schemas)
    for tool_name, arguments in transport.calls:
        _assert_value_matches_schema(arguments, schemas[tool_name])


def test_effect_result_decodes_the_server_envelope() -> None:
    """Effects expose a typed envelope while leaving tool-specific results opaque."""
    transport = RecordingTransport()
    result = EffectClient(transport).check_inbox()
    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.result is None
    assert result.error is None


def _load_schemas() -> dict[str, JsonObject]:
    raw = cast(JsonObject, json.loads(FIXTURE.read_text(encoding="utf-8")))
    tools = raw.get("tools")
    assert isinstance(tools, list)
    schemas: dict[str, JsonObject] = {}
    for tool in tools:
        assert isinstance(tool, dict)
        name = tool.get("name")
        input_schema = tool.get("inputSchema")
        assert isinstance(name, str)
        assert isinstance(input_schema, dict)
        schemas[name] = cast(JsonObject, input_schema)
    return schemas


def _invoke_sample_effects(client: EffectClient) -> None:
    """Exercise every method, including optional fields and nested dataclasses."""
    client.fork_wave(
        children=[ChildSpec(slug="child", task="sample", agent_type="codex", fork_session=False)]
    )
    client.spawn_leaf(
        name="leaf",
        task="sample",
        agent_type="codex",
        boundary=["avoid"],
        context="context",
        read_first=["README.md"],
        steps=["step"],
        verify=["just test"],
    )
    client.spawn_worker(name="worker", task="sample", agent_type="codex")
    client.spawn_reviewer(pr_number=1, force=False)
    client.cleanup_reviewer_leaf(pr_number=1)
    client.close_reviewer_window(pr_number=1)
    client.restart_review(pr_number=1)
    client.replace_close_pr(
        chainlink_issue_id=1,
        closed_pr_number=2,
        old_leaf_name="old",
        new_leaf_name="new",
        replacement_task="replace",
        human_approved=True,
        agent_type="codex",
        operator_context="approved",
    )
    client.resume_pr(
        pr_number=1,
        task="resume",
        boundary=["avoid"],
        context="context",
        done_criteria=["done"],
        read_first=["README.md"],
        steps=["step"],
        verify=["just test"],
    )
    client.watcher_pr_state(pr_number=1)
    client.close_worker_pane(pane_id="%1")
    client.spawn_codex(
        task="sample",
        branch_name="sample",
        agent_type="codex",
        allowed_dirs=["tl_loop"],
        allowed_tools=["just test"],
        disallowed_tools=["rm"],
        permission_mode="default",
        standalone_repo=False,
    )
    client.session_status(include_dead=True)
    client.poll_workers(include_dead=False, agents=["leaf"])
    client.check_inbox()
    client.memory_append(
        append_args_kind="decision",
        append_args_summary="summary",
        append_args_detail="detail",
        append_args_importance=3,
        append_args_issue_id=1,
    )
    client.memory_list(
        list_args_issue_id=1,
        list_args_kind="decision",
        list_args_limit=10,
        list_args_min_importance=2,
    )
    client.continuation_brief()
    client.list_agents(filter_type="codex")
    client.file_pr(title="title", body="body", base_branch="main")
    client.merge_pr(
        pr_number=1,
        chainlink_issue_id=2,
        force=False,
        strategy="squash",
        working_dir=".",
    )
    client.notify_parent(
        status="success",
        message="message",
        pr_number=1,
        tasks_completed=[CompletedTask(what="task", how="just test")],
    )
    client.send_tmux_message(recipient="parent", content="content", summary="summary")
    client.send_mailbox_message(recipient="parent", content="content", summary="summary")
    client.chainlink_issue_create(
        title="title",
        description="description",
        labels=["feature"],
        priority="medium",
    )
    client.chainlink_session_start()
    client.chainlink_session_status()
    client.chainlink_issue_show(issue_id=1)
    client.chainlink_issue_comment(issue_id=1, message="message")
    client.chainlink_subissue_create(
        parent_id=1,
        title="title",
        labels=["feature"],
        priority="medium",
    )
    client.chainlink_session_work(issue_id=1)
    client.chainlink_session_end(notes="notes")
    client.chainlink_issue_close(issue_id=1, force=False, summary="summary")
    client.close_issue_and_cleanup(issue_id=1, leaf_name="leaf")
    client.cleanup_orphan(name="leaf", dry_run=True)
    client.cleanup_leaf(dry_run=True, sweep=False, name="leaf")
    client.chainlink_timer_start(issue_id=1)
    client.chainlink_timer_stop(issue_id=1)
    client.chainlink_timer_status(issue_id=1)
    client.chainlink_issue_list(
        labels=["feature"],
        milestone="M1",
        priority="medium",
        status="open",
    )
    client.chainlink_issue_update(
        issue_id=1,
        labels=["feature"],
        milestone="M1",
        priority="high",
        status="in_progress",
    )
    client.chainlink_issue_block(child_id=2, blocker_id=1)
    client.chainlink_issue_relate(issue1=1, issue2=2)
    client.chainlink_issue_cascade(issue_id=1)
    client.chainlink_milestone_create(title="M1", description="description")
    client.chainlink_milestone_list()


def _assert_value_matches_schema(value: JsonValue, schema: JsonObject) -> None:
    schema_type = schema.get("type")
    enum_values = schema.get("enum")
    if enum_values is not None:
        assert isinstance(enum_values, list)
        assert value in enum_values

    if schema_type == "object":
        assert isinstance(value, dict)
        properties = schema.get("properties")
        assert isinstance(properties, dict)
        required = schema.get("required", [])
        assert isinstance(required, list)
        for key in required:
            assert isinstance(key, str)
            assert key in value
        for key, child in value.items():
            child_schema = properties.get(key)
            assert isinstance(child_schema, dict), key
            _assert_value_matches_schema(child, cast(JsonObject, child_schema))
    elif schema_type == "array":
        assert isinstance(value, list)
        items = schema.get("items")
        assert isinstance(items, dict)
        for item in value:
            _assert_value_matches_schema(item, cast(JsonObject, items))
    elif schema_type == "string":
        assert isinstance(value, str)
    elif schema_type == "integer":
        assert isinstance(value, int) and not isinstance(value, bool)
    elif schema_type == "boolean":
        assert isinstance(value, bool)
