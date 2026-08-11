"""Coverage for every deterministic difficulty-classification rule."""

from __future__ import annotations

from tl_loop.select.classify import Difficulty, classify_task
from tl_loop.state.schema import SliceState, SliceStatus


def test_focused_slice_is_trivial() -> None:
    result = classify_task(_slice(paths=("src/task.py",)))

    assert result == (Difficulty.TRIVIAL, "focused_slice")
    assert result.difficulty is Difficulty.TRIVIAL
    assert result.matched_rule_name == "focused_slice"


def test_missing_test_plan_is_standard() -> None:
    result = classify_task(_slice(paths=("src/task.py",), test_plan=()))

    assert result == (Difficulty.STANDARD, "missing_test_plan")


def test_multiple_paths_are_standard_at_the_narrow_boundary() -> None:
    result = classify_task(_slice(paths=("src/a.py", "src/b.py")))

    assert result == (Difficulty.STANDARD, "standard_slice")


def test_high_risk_review_path_is_hard() -> None:
    result = classify_task(_slice(paths=("rust/exomonad-core/src/handlers/events.rs",)))

    assert result == (Difficulty.HARD, "high_risk_path")


def test_proto_glob_is_high_risk() -> None:
    result = classify_task(_slice(paths=("proto/events.proto",)))

    assert result == (Difficulty.HARD, "high_risk_path")


def test_cross_language_paths_are_hard() -> None:
    result = classify_task(_slice(paths=("src/worker.py", "src/runtime.rs")))

    assert result == (Difficulty.HARD, "cross_language_span")


def test_four_paths_are_broad_and_hard() -> None:
    result = classify_task(
        _slice(paths=("src/a.py", "src/b.py", "src/c.py", "src/d.py"))
    )

    assert result == (Difficulty.HARD, "broad_path_scope")


def test_three_top_level_roots_are_broad_and_hard() -> None:
    result = classify_task(_slice(paths=("src/a.py", "tests/a.py", "docs/a.md")))

    assert result == (Difficulty.HARD, "broad_path_scope")


def test_dependency_fan_in_is_hard_at_three() -> None:
    result = classify_task(_slice(depends_on=("a", "b", "c")))

    assert result == (Difficulty.HARD, "dependency_fan_in")


def test_long_test_plan_is_hard_at_four_steps() -> None:
    result = classify_task(
        _slice(test_plan=("one", "two", "three", "four"))
    )

    assert result == (Difficulty.HARD, "long_test_plan")


def _slice(
    *,
    paths: tuple[str, ...] = ("src/task.py",),
    test_plan: tuple[str, ...] = ("pytest",),
    depends_on: tuple[str, ...] = (),
) -> SliceState:
    return SliceState(
        id="task",
        status=SliceStatus.PENDING,
        paths=paths,
        depends_on=depends_on,
        base_ref="main",
        test_plan=test_plan,
        agent_type=None,
        model=None,
        branch=None,
        worktree=None,
        pr_number=None,
        reviewed_head=None,
        attempts=0,
        verdict=None,
    )
