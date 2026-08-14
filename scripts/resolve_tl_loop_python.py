"""Print the controller interpreter selected by the shared policy."""

import argparse
import os
from pathlib import Path


def resolve(policy: Path, environ: dict[str, str] | None = None) -> str:
    values = {}
    for line in policy.read_text(encoding="utf-8").splitlines():
        if " = " in line and not line.lstrip().startswith("#"):
            key, value = line.split(" = ", 1)
            values[key] = value.strip().strip('"')
    environment = environ if environ is not None else os.environ
    return environment.get(values["environment"], "") or values["fallback"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, default=Path("tl_loop/interpreter_policy.toml"))
    arguments = parser.parse_args()
    print(resolve(arguments.policy))


if __name__ == "__main__":
    main()
