"""Cross-language contracts between the Haskell guest and Python TL loop."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest

from tl_loop.client.effects import ToolResult
from tl_loop.client.transport import JsonObject
from tl_loop.loop.driver import (
    AggregatePublicationRejected,
    _decode_aggregate_publication_lookup,
)


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


def test_production_agent_handler_artifact_is_accepted_by_aggregate_lookup_consumer() -> None:
    artifact_path = os.environ.get("EXOMONAD_PUBLICATION_LOOKUP_ARTIFACT")
    if artifact_path is None:
        pytest.skip("run through just test-wasm-integration to generate the production artifact")

    outputs = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    assert isinstance(outputs, dict)

    selected_raw = outputs["selected"]
    selected_result = ToolResult.from_raw(cast(JsonObject, selected_raw))
    lookup = _decode_aggregate_publication_lookup("slice-production", selected_result, 1)
    assert lookup is not None
    assert lookup.pr_number == 46
    assert dict(lookup.publication) == {
        "invocation_id": "invocation-selected",
        "slice_id": "slice-production",
        "author_agent": "stage-owner",
        "succession_invocation_ids": [
            "invocation-selected-resume-1",
            "invocation-selected-resume-2",
        ],
    }

    never_raw = outputs["never_published"]
    never_result = ToolResult.from_raw(cast(JsonObject, never_raw))
    assert _decode_aggregate_publication_lookup("slice-never-published", never_result, 1) is None
    assert never_result.result["publication"] is None  # type: ignore[index]

    abandoned_raw = outputs["all_attempts_abandoned"]
    abandoned_result = ToolResult.from_raw(cast(JsonObject, abandoned_raw))
    with pytest.raises(AggregatePublicationRejected) as rejected:
        _decode_aggregate_publication_lookup("slice-abandoned", abandoned_result, 1)
    assert rejected.value.error_kind == "publication_lookup_unresolved"
    assert abandoned_result.result["publication"] is None  # type: ignore[index]
