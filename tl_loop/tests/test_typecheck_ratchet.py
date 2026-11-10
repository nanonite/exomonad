from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location(
    "check_typecheck_ratchet",
    PROJECT_ROOT / "scripts/check_typecheck_ratchet.py",
)
assert SPEC and SPEC.loader
CHECKER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHECKER
SPEC.loader.exec_module(CHECKER)

VALID_SUMMARY = "Found 2 errors in 2 files (checked 4 source files)"


def _mypy_output(*lines: str, summary: str = VALID_SUMMARY) -> str:
    return "\n".join((*lines, summary)) + "\n"


def _report(
    counts: dict[tuple[str, str], int],
    *,
    checked_files: int = 4,
) -> object:
    return CHECKER.MypyReport(
        counts=counts,
        error_total=sum(counts.values()),
        checked_files=checked_files,
    )


def test_parse_counts_errors_and_ignores_notes() -> None:
    output = _mypy_output(
        'tl_loop/a.py:3: error: Incompatible types in assignment  [assignment]',
        'tl_loop/a.py:3: note: See "https://mypy.readthedocs.io"',
        'tl_loop/b.py:9: error: Function is missing a return type annotation  [no-untyped-def]',
    )

    report = CHECKER.parse_mypy_output(output)

    assert report.counts == {
        ("tl_loop/a.py", "assignment"): 1,
        ("tl_loop/b.py", "no-untyped-def"): 1,
    }
    assert report.error_total == 2
    assert report.checked_files == 4


def test_parse_attaches_indented_continuation_lines() -> None:
    output = _mypy_output(
        "tl_loop/a.py:3: error: Incompatible types in assignment",
        '    (expression has type "str", variable has type "int")  [assignment]',
        "tl_loop/a.py:3: note: The hinted line is not a separate error",
        summary="Found 1 error in 1 file (checked 4 source files)",
    )

    report = CHECKER.parse_mypy_output(output)

    assert report.counts == {("tl_loop/a.py", "assignment"): 1}
    assert report.error_total == 1


def test_parse_rejects_error_without_code() -> None:
    output = _mypy_output(
        "tl_loop/a.py:3: error: No code on this error",
        "Found 1 error in 1 file (checked 4 source files)",
    )

    with pytest.raises(CHECKER.MypyOutputError, match="has no error code"):
        CHECKER.parse_mypy_output(output)


def test_parse_rejects_unparsable_line() -> None:
    output = _mypy_output(
        "tl_loop/a.py:3: error: Bad assignment  [assignment]",
        "this line is not mypy output",
    )

    with pytest.raises(CHECKER.MypyOutputError, match="unparsable mypy output line"):
        CHECKER.parse_mypy_output(output)


def test_zero_files_checked_fails() -> None:
    with pytest.raises(CHECKER.MypyOutputError, match="checked zero files"):
        CHECKER.parse_mypy_output("Success: no issues found in 0 source files\n")


def test_mypy_crash_fails_even_with_parsed_errors() -> None:
    output = _mypy_output(
        "tl_loop/a.py:3: error: Bad assignment  [assignment]",
    )

    with pytest.raises(CHECKER.MypyOutputError, match="crash or blocked run"):
        CHECKER.analyze_mypy_output(2, output)


def test_missing_summary_fails() -> None:
    with pytest.raises(CHECKER.MypyOutputError, match="no completion summary"):
        CHECKER.analyze_mypy_output(
            1,
            "tl_loop/a.py:3: error: Bad assignment  [assignment]\n",
        )


def test_summary_count_mismatch_fails() -> None:
    output = _mypy_output(
        "tl_loop/a.py:3: error: Bad assignment  [assignment]",
        summary="Found 2 errors in 1 file (checked 4 source files)",
    )

    with pytest.raises(CHECKER.MypyOutputError, match="parsed 1"):
        CHECKER.parse_mypy_output(output)


def test_over_baseline_count_is_a_violation() -> None:
    violations = CHECKER.compare_counts(
        {("tl_loop/a.py", "assignment"): 2},
        {"tl_loop/a.py": {"assignment": 1}},
    )

    assert [(item.path, item.code, item.kind) for item in violations] == [
        ("tl_loop/a.py", "assignment", "over baseline")
    ]
    assert violations[0].baseline == 1
    assert violations[0].current == 2


def test_new_error_pair_is_a_violation() -> None:
    violations = CHECKER.compare_counts(
        {("tl_loop/new.py", "attr-defined"): 1},
        {},
    )

    assert [(item.path, item.code, item.kind) for item in violations] == [
        ("tl_loop/new.py", "attr-defined", "new error")
    ]
    assert violations[0].baseline == 0
    assert violations[0].current == 1


def test_under_baseline_count_is_a_violation() -> None:
    violations = CHECKER.compare_counts(
        {("tl_loop/a.py", "assignment"): 1},
        {"tl_loop/a.py": {"assignment": 2}},
    )

    assert [(item.path, item.code, item.kind) for item in violations] == [
        ("tl_loop/a.py", "assignment", "baseline higher than current")
    ]


def test_failure_output_names_file_code_and_counts() -> None:
    violations = CHECKER.compare_counts(
        {
            ("tl_loop/a.py", "assignment"): 2,
            ("tl_loop/b.py", "attr-defined"): 1,
        },
        {"tl_loop/a.py": {"assignment": 1}},
    )

    text = CHECKER.format_violations(violations, "check_typecheck_ratchet.py --update")

    assert "tl_loop/a.py [assignment]: baseline 1, current 2 (over baseline)" in text
    assert "tl_loop/b.py [attr-defined]: baseline 0, current 1 (new error)" in text


def test_update_lowers_counts_without_allow_increase() -> None:
    baseline = {"tl_loop/a.py": {"assignment": 2, "arg-type": 1}}

    updated = CHECKER.updated_baseline(
        {("tl_loop/a.py", "assignment"): 1, ("tl_loop/a.py", "arg-type"): 1},
        baseline,
        allow_increase=False,
    )

    assert updated == {"tl_loop/a.py": {"assignment": 1, "arg-type": 1}}


def test_update_refuses_to_raise_without_allow_increase() -> None:
    baseline = {"tl_loop/a.py": {"assignment": 1}}

    with pytest.raises(CHECKER.BaselineUpdateRefused) as raised:
        CHECKER.updated_baseline(
            {("tl_loop/a.py", "assignment"): 2, ("tl_loop/b.py", "attr-defined"): 1},
            baseline,
            allow_increase=False,
        )

    message = str(raised.value)
    assert "tl_loop/a.py [assignment]: baseline 1, current 2" in message
    assert "tl_loop/b.py [attr-defined]: baseline 0, current 1" in message


def test_update_allows_increase_only_with_flag() -> None:
    baseline = {"tl_loop/a.py": {"assignment": 1}}
    current = {("tl_loop/a.py", "assignment"): 2, ("tl_loop/b.py", "attr-defined"): 1}

    updated = CHECKER.updated_baseline(current, baseline, allow_increase=True)

    assert updated == {
        "tl_loop/a.py": {"assignment": 2},
        "tl_loop/b.py": {"attr-defined": 1},
    }


def test_baseline_writes_deterministically(tmp_path: Path) -> None:
    baseline = {
        "tl_loop/z.py": {"return-value": 2, "assignment": 1},
        "tl_loop/a.py": {"arg-type": 3},
    }
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    CHECKER.write_baseline(first, baseline, "2.3.0")
    CHECKER.write_baseline(second, baseline, "2.3.0")

    first_text = first.read_text(encoding="utf-8")
    assert first_text == second.read_text(encoding="utf-8")
    payload = json.loads(first_text)
    assert payload["schema_version"] == 2
    assert payload["mypy_version"] == "2.3.0"
    assert payload["errors"] == baseline
    assert list(payload["errors"]) == ["tl_loop/a.py", "tl_loop/z.py"]
    assert list(payload["errors"]["tl_loop/z.py"]) == ["assignment", "return-value"]
    assert first_text.endswith("\n")


def test_baseline_rejects_schema_one(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps({"schema_version": 1, "mypy_version": "2.3.0", "errors": {}}),
        encoding="utf-8",
    )

    with pytest.raises(CHECKER.BaselineError, match="schema_version 2"):
        CHECKER.load_baseline(baseline_path)


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "errors": {}},
        {"schema_version": 2, "mypy_version": 2, "errors": {}},
        {"schema_version": 2, "mypy_version": "", "errors": {}},
    ],
)
def test_baseline_rejects_missing_or_non_string_version(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CHECKER.BaselineError, match="mypy_version string"):
        CHECKER.load_baseline(baseline_path)


def test_version_mismatch_fails_before_count_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline_path = tmp_path / "baseline.json"
    CHECKER.write_baseline(
        baseline_path, {"tl_loop/a.py": {"assignment": 1}}, "0.0.1"
    )
    monkeypatch.setattr(
        CHECKER,
        "run_mypy",
        _constant_report({("tl_loop/a.py", "assignment"): 2}),
    )
    monkeypatch.setattr(CHECKER, "installed_mypy_version", lambda: "0.0.2")

    exit_code = CHECKER.main(
        ["--project-root", str(tmp_path), "--baseline", str(baseline_path)]
    )

    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "0.0.1" in stderr
    assert "0.0.2" in stderr
    assert "pyproject.toml" in stderr
    assert "--update" in stderr
    assert "over baseline" not in stderr


def test_matching_version_compares_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline_path = tmp_path / "baseline.json"
    CHECKER.write_baseline(
        baseline_path, {"tl_loop/a.py": {"assignment": 1}}, "0.0.1"
    )
    monkeypatch.setattr(
        CHECKER,
        "run_mypy",
        _constant_report({("tl_loop/a.py", "assignment"): 1}),
    )
    monkeypatch.setattr(CHECKER, "installed_mypy_version", lambda: "0.0.1")

    exit_code = CHECKER.main(
        ["--project-root", str(tmp_path), "--baseline", str(baseline_path)]
    )

    assert exit_code == 0
    assert "typecheck ratchet passed" in capsys.readouterr().out


def test_update_records_installed_version_across_a_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline_path = tmp_path / "baseline.json"
    CHECKER.write_baseline(
        baseline_path, {"tl_loop/a.py": {"assignment": 2}}, "0.0.1"
    )
    monkeypatch.setattr(
        CHECKER,
        "run_mypy",
        _constant_report({("tl_loop/a.py", "assignment"): 1}),
    )
    monkeypatch.setattr(CHECKER, "installed_mypy_version", lambda: "0.0.2")

    exit_code = CHECKER.main(
        [
            "--project-root",
            str(tmp_path),
            "--baseline",
            str(baseline_path),
            "--update",
        ]
    )

    assert exit_code == 0
    payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["mypy_version"] == "0.0.2"
    assert payload["errors"] == {"tl_loop/a.py": {"assignment": 1}}
    assert "baseline written" in capsys.readouterr().out


def test_update_refuses_increase_across_a_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline_path = tmp_path / "baseline.json"
    CHECKER.write_baseline(
        baseline_path, {"tl_loop/a.py": {"assignment": 1}}, "0.0.1"
    )
    monkeypatch.setattr(
        CHECKER,
        "run_mypy",
        _constant_report({("tl_loop/a.py", "assignment"): 2}),
    )
    monkeypatch.setattr(CHECKER, "installed_mypy_version", lambda: "0.0.2")

    exit_code = CHECKER.main(
        [
            "--project-root",
            str(tmp_path),
            "--baseline",
            str(baseline_path),
            "--update",
        ]
    )

    assert exit_code == 1
    assert "--allow-increase" in capsys.readouterr().err
    payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert payload["mypy_version"] == "0.0.1"
    assert payload["errors"] == {"tl_loop/a.py": {"assignment": 1}}


def test_missing_mypy_reports_import_failure_before_reading_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline_path = tmp_path / "baseline.json"
    CHECKER.write_baseline(
        baseline_path, {"tl_loop/a.py": {"assignment": 1}}, "2.3.0"
    )

    def fail_run(_root: Path, _config: Path, _target: Path) -> object:
        raise CHECKER.MypyOutputError(
            "unparsable mypy output line: '/usr/bin/python3: No module named mypy'"
        )

    def unexpected_version() -> str:
        raise AssertionError("metadata must not be read when mypy is missing")

    monkeypatch.setattr(CHECKER, "run_mypy", fail_run)
    monkeypatch.setattr(CHECKER, "installed_mypy_version", unexpected_version)

    exit_code = CHECKER.main(
        ["--project-root", str(tmp_path), "--baseline", str(baseline_path)]
    )

    assert exit_code == 1
    assert "No module named mypy" in capsys.readouterr().err


def test_main_reports_baseline_violation_and_update_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline_path = tmp_path / "baseline.json"
    CHECKER.write_baseline(
        baseline_path, {"tl_loop/a.py": {"assignment": 1}}, "2.3.0"
    )
    monkeypatch.setattr(CHECKER, "installed_mypy_version", lambda: "2.3.0")
    monkeypatch.setattr(
        CHECKER,
        "run_mypy",
        _constant_report({("tl_loop/a.py", "assignment"): 2}),
    )

    check_exit = CHECKER.main(
        ["--project-root", str(tmp_path), "--baseline", str(baseline_path)]
    )

    assert check_exit == 1
    assert (
        "tl_loop/a.py [assignment]: baseline 1, current 2 (over baseline)"
        in capsys.readouterr().err
    )

    update_exit = CHECKER.main(
        [
            "--project-root",
            str(tmp_path),
            "--baseline",
            str(baseline_path),
            "--update",
        ]
    )

    assert update_exit == 1
    assert "--allow-increase" in capsys.readouterr().err

    allow_exit = CHECKER.main(
        [
            "--project-root",
            str(tmp_path),
            "--baseline",
            str(baseline_path),
            "--update",
            "--allow-increase",
        ]
    )

    assert allow_exit == 0
    payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert payload["errors"] == {"tl_loop/a.py": {"assignment": 2}}
    assert payload["mypy_version"] == "2.3.0"
    assert "baseline written" in capsys.readouterr().out


def _constant_report(
    counts: dict[tuple[str, str], int],
) -> Callable[[Path, Path, Path], object]:
    def run(_root: Path, _config: Path, _target: Path) -> object:
        return _report(counts)

    return run
