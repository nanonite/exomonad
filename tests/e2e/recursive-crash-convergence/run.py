#!/usr/bin/env python3
"""Chainlink #1057 real-server crash and restart acceptance."""

from __future__ import annotations

import argparse
import json

from runner import run_matrix


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("server",), default="server")
    parser.parse_args()
    print(json.dumps({"server": run_matrix()}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
