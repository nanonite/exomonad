"""Documentation examples are executable plan-contract fixtures."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tl_loop.plan_validation import validate_plan_document

GUIDE = Path(__file__).parents[2] / "docs/guides/programming-the-tl.md"


def test_ordered_plan_examples_validate_against_the_plan_schema() -> None:
    document = GUIDE.read_text(encoding="utf-8")
    examples_section = document.split("### Ordered plan examples", 1)[1].split(
        "### Recursive integration and recovery", 1
    )[0]
    examples = re.findall(r"~~~json\n(.*?)\n~~~", examples_section, flags=re.DOTALL)

    assert len(examples) == 4
    for example in examples:
        validated = validate_plan_document(json.loads(example))
        assert validated["plan"]
