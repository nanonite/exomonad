"""Cross-language contracts between the Haskell guest and Python TL loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from tl_loop.client.effects import ToolResult
from tl_loop.client.transport import JsonObject
from tl_loop.loop.driver import _decode_aggregate_publication_lookup


def test_haskell_projection_fixture_is_accepted_by_aggregate_lookup_consumer() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "haskell/wasm-guest/test/fixtures/resolve_live_pr_for_slice_tool_result.json"
    )
    raw = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)

    result = ToolResult.from_raw(cast(JsonObject, raw))
    lookup = _decode_aggregate_publication_lookup('slice-contract-雪/"stage', result, 1)

    assert lookup is not None
    assert lookup.pr_number == 46
    assert lookup.publication == {
        "invocation_id": 'invocation-1205-雪/"head',
        "slice_id": 'slice-contract-雪/"stage',
        "author_agent": 'stage-agent-雪/"owner',
        "succession_invocation_ids": [
            "invocation-original-1205",
            "invocation-resumed-1205",
        ],
    }
