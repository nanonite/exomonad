#!/usr/bin/env python3
"""Fail when strict mypy errors grow or the checked-in baseline is stale.

The contract is ``tl_loop/pyproject.toml``.  The checker runs exactly that
configuration:

    mypy --config-file tl_loop/pyproject.toml tl_loop

from the repository root.  The baseline records per-file, per-error-code
counts and the mypy version that produced them.  The counts may shrink, but
they may not grow without an explicit ``--allow-increase``, and the check
fails when the installed mypy differs from the recorded version.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = Path("tl_loop/pyproject.toml")
DEFAULT_TARGET = Path("tl_loop")
DEFAULT_BASELINE = Path("tl_loop/typecheck-baseline.json")
SCHEMA_VERSION = 2
MYPY_TIMEOUT_SECONDS = 1800

_DIAGNOSTIC = re.compile(
    r"^(?P<path>.+?):(?:(?P<line>\d+): )?"
    r"(?P<severity>error|note|warning): (?P<message>.*)$"
)
_ERROR_SUMMARY = re.compile(
    r"^Found (?P<errors>\d+) errors? in (?P<files>\d+) files? "
    r"\(checked (?P<checked>\d+) source files?\)$"
)
_BLOCKED_SUMMARY = re.compile(
    r"^Found (?P<errors>\d+) errors? in (?P<files>\d+) files? "
    r"\(errors prevented further checking\)$"
)
_SUCCESS_SUMMARY = re.compile(
    r"^Success: no issues found in (?P<checked>\d+) source files?$"
)
_ERROR_CODE = re.compile(r"  \[(?P<code>[A-Za-z0-9_-]+)\]$")
_CONTINUATION = re.compile(r"^\s+\S")

CountKey = tuple[str, str]
Counts = Mapping[CountKey, int]
BaselineCounts = dict[str, dict[str, int]]


class RatchetError(Exception):
    """Base class for expected ratchet failures."""


class MypyOutputError(RatchetError):
    """Mypy did not produce a complete, parseable strict run."""


class BaselineError(RatchetError):
    """The baseline file is missing or malformed."""


class BaselineUpdateRefused(RatchetError):
    """The requested update would raise a baseline count."""


@dataclass(frozen=True)
class MypyReport:
    counts: Mapping[CountKey, int]
    error_total: int
    checked_files: int


@dataclass(frozen=True)
class Baseline:
    mypy_version: str
    errors: BaselineCounts


@dataclass(frozen=True)
class Violation:
    path: str
    code: str
    baseline: int
    current: int

    @property
    def kind(self) -> str:
        if self.baseline == 0:
            return "new error"
        if self.current > self.baseline:
            return "over baseline"
        return "baseline higher than current"


def _normalize_path(path: str, project_root: Path | None) -> str:
    candidate = Path(path)
    if candidate.is_absolute() and project_root is not None:
        try:
            candidate = candidate.relative_to(project_root)
        except ValueError:
            pass
    normalized = candidate.as_posix()
    return normalized.removeprefix("./")


def _extract_code(message_lines: Sequence[str]) -> str | None:
    code: str | None = None
    for line in message_lines:
        match = _ERROR_CODE.search(line)
        if match:
            code = match.group("code")
    return code


def _finish_error(
    counts: Counter[CountKey],
    path: str | None,
    message_lines: list[str],
) -> None:
    if path is None:
        return
    code = _extract_code(message_lines)
    if code is None:
        first_line = message_lines[0] if message_lines else ""
        raise MypyOutputError(f"mypy error for {path} has no error code: {first_line}")
    counts[(path, code)] += 1


def parse_mypy_output(output: str, project_root: Path | None = None) -> MypyReport:
    """Parse one complete mypy run, including notes and continuation lines."""
    counts: Counter[CountKey] = Counter()
    checked_files: int | None = None
    summary_errors: int | None = None
    saw_summary = False
    current_path: str | None = None
    current_message: list[str] = []

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        diagnostic = _DIAGNOSTIC.match(line)
        if diagnostic is not None:
            _finish_error(counts, current_path, current_message)
            current_path = None
            current_message = []
            if diagnostic.group("severity") == "error":
                current_path = _normalize_path(diagnostic.group("path"), project_root)
                current_message = [diagnostic.group("message")]
            continue

        success = _SUCCESS_SUMMARY.fullmatch(line)
        if success is not None:
            _finish_error(counts, current_path, current_message)
            current_path = None
            current_message = []
            checked_files = int(success.group("checked"))
            saw_summary = True
            continue

        errors = _ERROR_SUMMARY.fullmatch(line)
        if errors is not None:
            _finish_error(counts, current_path, current_message)
            current_path = None
            current_message = []
            summary_errors = int(errors.group("errors"))
            checked_files = int(errors.group("checked"))
            saw_summary = True
            continue

        if _BLOCKED_SUMMARY.fullmatch(line) is not None:
            raise MypyOutputError(f"mypy stopped before checking all files: {line}")

        if not line:
            if current_path is not None:
                current_message.append("")
            continue

        if current_path is not None and _CONTINUATION.match(raw_line):
            current_message.append(line)
            continue

        raise MypyOutputError(f"unparsable mypy output line: {raw_line!r}")

    _finish_error(counts, current_path, current_message)
    if not saw_summary:
        raise MypyOutputError("mypy produced no completion summary")
    if checked_files is None or checked_files == 0:
        raise MypyOutputError("mypy checked zero files")
    if summary_errors is not None and summary_errors != sum(counts.values()):
        raise MypyOutputError(
            "mypy summary reports "
            f"{summary_errors} errors but output parsed {sum(counts.values())}"
        )
    if summary_errors is None and counts:
        raise MypyOutputError("mypy reported success after emitting errors")
    return MypyReport(
        counts=dict(counts),
        error_total=sum(counts.values()),
        checked_files=checked_files,
    )


def analyze_mypy_output(
    exit_code: int,
    output: str,
    project_root: Path | None = None,
) -> MypyReport:
    """Validate the process result before trusting its diagnostics."""
    if exit_code not in (0, 1):
        raise MypyOutputError(f"mypy exited with status {exit_code} (crash or blocked run)")
    report = parse_mypy_output(output, project_root)
    if exit_code == 0 and report.error_total:
        raise MypyOutputError("mypy exited 0 after reporting errors")
    if exit_code == 1 and report.error_total == 0:
        raise MypyOutputError("mypy exited 1 without reporting errors")
    return report


def run_mypy(project_root: Path, config_file: Path, target: Path) -> MypyReport:
    """Run the strict contract from the repository root."""
    resolved_config = _resolve(project_root, config_file)
    resolved_target = _resolve(project_root, target)
    if not resolved_config.is_file():
        raise MypyOutputError(f"mypy config not found: {resolved_config}")
    if not resolved_target.exists():
        raise MypyOutputError(f"mypy target not found: {resolved_target}")
    command = [
        sys.executable,
        "-m",
        "mypy",
        "--config-file",
        str(config_file),
        str(target),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=MYPY_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MypyOutputError(f"cannot run mypy: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MypyOutputError("mypy timed out") from exc
    output = completed.stdout
    if completed.stderr:
        output = f"{output}\n{completed.stderr}"
    return analyze_mypy_output(completed.returncode, output, project_root)


def flatten_baseline(baseline: BaselineCounts) -> dict[CountKey, int]:
    return {
        (path, code): count
        for path, codes in baseline.items()
        for code, count in codes.items()
    }


def baseline_from_counts(counts: Counts) -> BaselineCounts:
    baseline: BaselineCounts = {}
    for (path, code), count in sorted(counts.items()):
        baseline.setdefault(path, {})[code] = count
    return baseline


def load_baseline(path: Path) -> Baseline:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BaselineError(f"cannot read baseline {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BaselineError(f"baseline {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise BaselineError(f"baseline {path} must be a JSON object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise BaselineError(
            f"baseline {path} must declare schema_version {SCHEMA_VERSION}"
        )
    mypy_version = payload.get("mypy_version")
    if not isinstance(mypy_version, str) or not mypy_version:
        raise BaselineError(f"baseline {path} must contain a mypy_version string")
    errors = payload.get("errors")
    if not isinstance(errors, dict):
        raise BaselineError(f"baseline {path} must contain an errors object")
    baseline: BaselineCounts = {}
    for file_name, codes in errors.items():
        if not isinstance(file_name, str) or not file_name:
            raise BaselineError(f"baseline {path} contains an invalid file name")
        if not isinstance(codes, dict):
            raise BaselineError(f"baseline {path} entry {file_name!r} must be an object")
        file_counts: dict[str, int] = {}
        for code, count in codes.items():
            if not isinstance(code, str) or not code:
                raise BaselineError(f"baseline {path} entry {file_name!r} has an invalid code")
            if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
                raise BaselineError(
                    f"baseline {path} entry {file_name!r} [{code}] must be a positive integer"
                )
            file_counts[code] = count
        baseline[file_name] = file_counts
    return Baseline(mypy_version=mypy_version, errors=baseline)


def write_baseline(path: Path, baseline: BaselineCounts, mypy_version: str) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "mypy_version": mypy_version,
        "errors": baseline,
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def installed_mypy_version() -> str:
    try:
        return importlib.metadata.version("mypy")
    except importlib.metadata.PackageNotFoundError as exc:
        raise BaselineError(
            "cannot determine the installed mypy version: No module named mypy"
        ) from exc


def check_mypy_version(installed: str, baseline_version: str) -> None:
    if installed == baseline_version:
        return
    raise BaselineError(
        f"baseline was generated by mypy {baseline_version} but the installed "
        f"mypy is {installed}: install mypy=={baseline_version}, or raise the "
        "pin in tl_loop/pyproject.toml and regenerate the baseline with --update "
        "in the same commit"
    )


def compare_counts(current: Counts, baseline: BaselineCounts) -> list[Violation]:
    baseline_counts = flatten_baseline(baseline)
    keys = sorted(set(current) | set(baseline_counts))
    return [
        Violation(
            path=path,
            code=code,
            baseline=baseline_counts.get((path, code), 0),
            current=current.get((path, code), 0),
        )
        for path, code in keys
        if current.get((path, code), 0) != baseline_counts.get((path, code), 0)
    ]


def format_violations(violations: Sequence[Violation], update_command: str) -> str:
    lines = [
        f"  {item.path} [{item.code}]: baseline {item.baseline}, "
        f"current {item.current} ({item.kind})"
        for item in violations
    ]
    if any(item.kind == "baseline higher than current" for item in violations):
        lines.append(f"Run {update_command} to lock in improvements.")
    return "\n".join(lines)


def updated_baseline(
    current: Counts,
    baseline: BaselineCounts,
    *,
    allow_increase: bool,
) -> BaselineCounts:
    violations = compare_counts(current, baseline)
    increases = [item for item in violations if item.current > item.baseline]
    if increases and not allow_increase:
        lines = [
            f"  {item.path} [{item.code}]: baseline {item.baseline}, "
            f"current {item.current}"
            for item in increases
        ]
        raise BaselineUpdateRefused(
            "refusing to raise baseline counts without --allow-increase:\n"
            + "\n".join(lines)
        )
    return baseline_from_counts(current)


def _resolve(project_root: Path, path: Path) -> Path:
    return path if path.is_absolute() else project_root / path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--allow-increase", action="store_true")
    args = parser.parse_args(argv)
    if args.allow_increase and not args.update:
        parser.error("--allow-increase requires --update")

    project_root = args.project_root.resolve()
    config_file = args.config_file
    target = args.target
    baseline_path = _resolve(project_root, args.baseline)
    update_command = f"{Path(sys.argv[0]).name} --update"

    try:
        report = run_mypy(project_root, config_file, target)
        mypy_version = installed_mypy_version()
        if args.update:
            baseline = load_baseline(baseline_path) if baseline_path.exists() else None
            updated = updated_baseline(
                report.counts,
                baseline.errors if baseline is not None else {},
                allow_increase=args.allow_increase,
            )
            write_baseline(baseline_path, updated, mypy_version)
            print(
                f"typecheck ratchet baseline written: {report.error_total} errors "
                f"across {len(updated)} files -> {baseline_path}"
            )
            return 0
        baseline = load_baseline(baseline_path)
        check_mypy_version(mypy_version, baseline.mypy_version)
        violations = compare_counts(report.counts, baseline.errors)
    except RatchetError as exc:
        print(f"typecheck ratchet failed: {exc}", file=sys.stderr)
        return 1

    if violations:
        print(
            f"typecheck ratchet failed: {len(violations)} baseline violation(s)",
            file=sys.stderr,
        )
        print(format_violations(violations, update_command), file=sys.stderr)
        return 1
    print(
        f"typecheck ratchet passed: {report.error_total} errors in "
        f"{report.checked_files} source files match {baseline_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
