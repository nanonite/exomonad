"""Best-effort controller observability effects."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import cast

from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import JsonObject

LOGGER = logging.getLogger(__name__)


def emit_controller_event(
    client: EffectClient,
    event_type: str,
    payload: Mapping[str, object],
) -> None:
    """Forward bounded dimensions without making the state transition fail."""
    try:
        result = client.emit_controller_event(
            event_type=event_type,
            payload=cast(JsonObject, dict(payload)),
        )
    except Exception as error:  # noqa: BLE001 - observability is fail-open
        LOGGER.warning("controller event %s failed: %s", event_type, error)
        return
    if result.success is False:
        LOGGER.warning(
            "controller event %s failed: %s",
            event_type,
            result.error or "effect returned failure",
        )
