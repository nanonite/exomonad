"""Validate the selected controller interpreter against pyproject metadata."""

import re
import sys
from pathlib import Path


def main() -> None:
    text = Path("tl_loop/pyproject.toml").read_text()
    requirement = re.search(r'^requires-python\s*=\s*"(>=\d+\.\d+)"\s*$', text, re.MULTILINE)
    if requirement is None:
        raise SystemExit("ERROR: tl_loop/pyproject.toml must declare requires-python")
    needed = tuple(map(int, requirement.group(1)[2:].split(".")))
    found = sys.version_info[:2]
    print(f"TL controller build interpreter: Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}; needed >= {needed[0]}.{needed[1]}")
    if found < needed:
        raise SystemExit(f"ERROR: TL controller needs Python >= {needed[0]}.{needed[1]}, found Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")


if __name__ == "__main__":
    main()
