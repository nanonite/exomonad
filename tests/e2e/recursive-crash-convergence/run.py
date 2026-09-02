#!/usr/bin/env python3
"""Chainlink #1057 real-server crash and captured-checkpoint acceptance."""

from __future__ import annotations

import argparse
import json

from beast import run_three_continuations
from runner import run_matrix


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("server", "beast", "all"), default="all")
    args = parser.parse_args()
    result: dict[str, object] = {}
    if args.mode in {"server", "all"}:
        result["server"] = run_matrix()
    if args.mode in {"beast", "all"}:
        result["beast"] = run_three_continuations()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
