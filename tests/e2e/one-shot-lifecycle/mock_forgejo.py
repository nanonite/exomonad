#!/usr/bin/env python3
"""Forgejo-compatible E2E mock with one controllable stale-SHA response."""

from __future__ import annotations

import argparse
import re
import signal
import sys
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mock_github import GitHubMockHandler, state

RACE: dict[str, object] = {}


class LifecycleMockHandler(GitHubMockHandler):
    """Reuse the repository-backed mock and add a deterministic SHA race."""

    def do_GET(self) -> None:
        path = self._path_only()
        match = re.match(r"^/api/v1/repos/[^/]+/[^/]+/pulls/(\d+)$", path)
        pr_number = int(match.group(1)) if match else None
        race_pr = RACE.get("pr_number")
        if pr_number is not None and pr_number == race_pr:
            step = int(RACE.get("step", 0))
            sha_key = "stale_sha" if step == 0 else "fresh_sha"
            state.prs[pr_number]["head"]["sha"] = str(RACE[sha_key])
            RACE["step"] = step + 1
            original_update = state.update_pr_sha
            state.update_pr_sha = lambda _number: None
            try:
                return super().do_GET()
            finally:
                state.update_pr_sha = original_update
        return super().do_GET()

    def do_POST(self) -> None:
        if self._path_only() == "/_control/stale_once":
            payload = self._read_body()
            RACE.clear()
            RACE.update(
                pr_number=int(payload["pr_number"]),
                stale_sha=str(payload["stale_sha"]),
                fresh_sha=str(payload["fresh_sha"]),
                step=0,
            )
            return self._send_json({"armed": True})
        return super().do_POST()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    server = HTTPServer((args.host, args.port), LifecycleMockHandler)

    def stop(_signal: int, _frame: object) -> None:
        server.server_close()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(f"Lifecycle Forgejo mock listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
