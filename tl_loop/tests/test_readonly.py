"""Structural write protection for shadow-mode effects."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import MUTATING_METHODS, MutationBlocked, ReadOnlyEffectClient
from tl_loop.client.transport import JsonObject


@dataclass
class RecordingTransport:
    calls: list[str] = field(default_factory=list)

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        del role, name, arguments
        self.calls.append(tool_name)
        return {"success": True, "result": None}


def test_every_mutating_effect_is_blocked() -> None:
    client = ReadOnlyEffectClient(EffectClient(RecordingTransport()))

    for method_name in sorted(MUTATING_METHODS):
        with pytest.raises(MutationBlocked, match=method_name):
            getattr(client, method_name)()


def test_read_effects_are_forwarded() -> None:
    transport = RecordingTransport()
    client = ReadOnlyEffectClient(EffectClient(transport))

    result = client.chainlink_issue_show(issue_id=708)

    assert result.success is True
    assert transport.calls == ["chainlink_issue_show"]
