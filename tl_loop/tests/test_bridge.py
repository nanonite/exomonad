"""Observational bridge mapping and lifecycle logging coverage."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import cast

import pytest

from tl_loop.events.bridge import BridgeError, bridge_event

FIXTURE = Path(__file__).parent / "fixtures" / "ledger_projection_events.json"


def test_review_bridge_preserves_reviewed_head_and_logs_lifecycle(caplog: pytest.LogCaptureFixture) -> None:
    events = cast(list[dict[str, object]], json.loads(FIXTURE.read_text(encoding="utf-8")))
    review = next(event for event in events if event["type"] == "copilot.review")
    logger = logging.getLogger("tl_loop.test_bridge")

    with caplog.at_level(logging.INFO, logger=logger.name):
        envelope = bridge_event(review, logger=logger)

    assert envelope.kind.value == "copilot.review"
    assert envelope.pr_number == 101
    assert envelope.reviewed_head == "bbb222"
    messages = [record.getMessage() for record in caplog.records]
    assert any(message.startswith("bridge before") for message in messages)
    assert any(message.startswith("bridge after") for message in messages)


def test_bridge_logs_and_types_projection_errors(caplog: pytest.LogCaptureFixture) -> None:
    events = cast(list[dict[str, object]], json.loads(FIXTURE.read_text(encoding="utf-8")))
    unmapped = dict(events[0])
    unmapped["type"] = "agent.guidance.delivery"
    logger = logging.getLogger("tl_loop.test_bridge_error")

    with caplog.at_level(logging.INFO, logger=logger.name), pytest.raises(BridgeError, match="agent.guidance.delivery"):
        bridge_event(unmapped, logger=logger)

    assert any(record.getMessage().startswith("bridge error") for record in caplog.records)
