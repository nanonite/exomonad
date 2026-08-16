"""Keep the complete ordered-plan examples executable as documentation."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tl_loop.plan_validation import validate_plan_document


def test_programming_guide_ordered_examples_validate() -> None:
    guide = Path(__file__).parents[2] / "docs/guides/programming-the-tl.md"
    blocks = re.findall(r"~~~json\n(.*?)\n~~~", guide.read_text(encoding="utf-8"), re.DOTALL)

    assert len(blocks) == 4
    for block in blocks:
        validate_plan_document(json.loads(block))
