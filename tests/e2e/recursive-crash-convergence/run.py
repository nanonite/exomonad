#!/usr/bin/env python3
"""Chainlink #1057 real-server crash and restart acceptance.

The run provisions everything it needs -- its own Forgejo, its own repository,
database, directory, and tmux servers per case -- so it takes no operator input
and needs no shared instance. ``--mode server`` is the only mode: every case
runs against a real server this run started against a real forge this run
brought up.
"""

from __future__ import annotations

import argparse
import json

from runner import DEFAULT_REPETITIONS, run_matrix


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the #1057 crash matrix.")
    parser.add_argument(
        "--mode",
        choices=("server",),
        default="server",
        help=(
            "the matrix always runs against a real server this run provisions; "
            "kept so the documented invocation keeps working and so a second "
            "mode cannot be added without a decision about what it owns"
        ),
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=DEFAULT_REPETITIONS,
        help=(
            "how many times to walk the whole matrix (default: "
            f"{DEFAULT_REPETITIONS}); 1 is a diagnostic pass, not the "
            "acceptance configuration"
        ),
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave this run's Forgejo, sessions, and directory for inspection",
    )
    arguments = parser.parse_args()
    report = run_matrix(repetitions=arguments.repetitions, keep=arguments.keep)
    report.evidence["mode"] = arguments.mode
    print(json.dumps(report.evidence, indent=2, sort_keys=True, default=str)[:20000])
    print(json.dumps(report.operation_totals, indent=2, sort_keys=True)[:4000])
    report.emit()
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())