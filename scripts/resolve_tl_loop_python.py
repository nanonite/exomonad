"""Print the controller interpreter selected by the shared policy."""

import os
from pathlib import Path


def main() -> None:
    values = {}
    for line in Path("tl_loop/interpreter_policy.toml").read_text().splitlines():
        if " = " in line and not line.lstrip().startswith("#"):
            key, value = line.split(" = ", 1)
            values[key] = value.strip().strip('"')
    print(os.environ.get(values["environment"], "") or values["fallback"])


if __name__ == "__main__":
    main()
