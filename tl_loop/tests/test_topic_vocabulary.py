"""Guard the obs topic view against event-registry drift."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs" / "observability" / "event-registry.json"
MAPPINGS = ROOT / "docs" / "observability" / "topic-mappings.v1.json"


def _object(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def test_every_obs_topic_has_an_existing_registry_counterpart() -> None:
    registry = _object(REGISTRY)
    mappings = _object(MAPPINGS)
    events = cast(list[dict[str, object]], registry["event_types"])
    event_types = {cast(str, event["type"]) for event in events}
    topics = cast(list[dict[str, object]], mappings["obs_topics"])

    assert topics
    assert all(topic["topic"].startswith("obs/") for topic in topics)
    assert all(topic["event_type"] in event_types for topic in topics)
    assert all(topic["topic"] == f"obs/event/{topic['event_type']}" for topic in topics)
