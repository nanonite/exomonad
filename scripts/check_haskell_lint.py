"""Reject HLint findings added to owned Haskell code beyond the reviewed baseline."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "scripts/hlint-owned-baseline.json"
OWNED_SCOPE = "haskell/wasm-guest"


def _finding_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    marker = f"{OWNED_SCOPE}/"
    if normalized.startswith(marker):
        return normalized
    if f"/{marker}" in normalized:
        return marker + normalized.split(f"/{marker}", 1)[1]
    raise ValueError(f"HLint finding escaped owned scope: {value}")


def _key(finding: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        _finding_path(str(finding["file"])),
        str(finding["hint"]),
        str(finding["from"]),
        str(finding["to"]),
    )


def _hlint_version(executable: str) -> str:
    result = subprocess.run(
        [executable, "--version"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"hlint --version failed: {result.stderr.strip()}")
    version_line = result.stdout.strip().splitlines()[0]
    if not version_line.startswith("HLint v"):
        raise RuntimeError(f"unrecognized HLint version output: {version_line}")
    return version_line.removeprefix("HLint v").split(",", 1)[0]


def _current_findings(executable: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        [executable, "--json", OWNED_SCOPE],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"hlint exited {result.returncode}: {result.stderr.strip()}"
        )
    findings = json.loads(result.stdout or "[]")
    if not isinstance(findings, list):
        raise TypeError("HLint JSON output must be an array")
    return findings


def _load_baseline() -> dict[str, Any]:
    with BASELINE_PATH.open(encoding="utf-8") as stream:
        baseline = json.load(stream)
    required = {"schema_version", "baseline_commit", "hlint_version", "findings"}
    if not isinstance(baseline, dict) or not required.issubset(baseline):
        raise ValueError(f"invalid HLint baseline: {BASELINE_PATH}")
    if baseline["schema_version"] != 1 or not isinstance(baseline["findings"], list):
        raise ValueError(f"unsupported HLint baseline: {BASELINE_PATH}")
    return baseline


def _new_findings(
    current: list[dict[str, Any]], baseline: list[dict[str, Any]]
) -> Counter[tuple[str, str, str, str]]:
    current_keys = Counter(_key(finding) for finding in current)
    baseline_keys = Counter(_key(finding) for finding in baseline)
    return current_keys - baseline_keys


def main() -> int:
    executable = shutil.which("hlint")
    if executable is None:
        print("ERROR: hlint is not available in PATH", file=sys.stderr)
        return 2
    try:
        baseline = _load_baseline()
        version = _hlint_version(executable)
        if version != baseline["hlint_version"]:
            raise RuntimeError(
                f"HLint version changed: baseline is {baseline['hlint_version']}, "
                f"installed is {version}; review the baseline before updating it"
            )
        current = _current_findings(executable)
        new = _new_findings(current, baseline["findings"])
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    if new:
        remaining = new.copy()
        print(f"FAIL: {sum(new.values())} new HLint finding(s) in {OWNED_SCOPE}")
        for finding in current:
            key = _key(finding)
            if remaining[key] <= 0:
                continue
            print(
                f"{key[0]}:{finding.get('startLine', 0)}:"
                f"{finding.get('startColumn', 0)}: {key[1]}"
            )
            remaining[key] -= 1
        return 1

    resolved = sum(
        (Counter(_key(finding) for finding in baseline["findings"])
         - Counter(_key(finding) for finding in current)).values()
    )
    print(
        f"PASS: {len(current)} owned-source HLint finding(s) match the reviewed "
        f"baseline from {baseline['baseline_commit']}; 0 new, {resolved} resolved"
    )
    print(
        "Separate generated/vendor scopes: haskell/proto and haskell/vendor "
        "are excluded from this owned-source gate"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
