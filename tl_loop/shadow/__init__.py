"""Shadow intent recording, actual-call extraction, and divergence reports."""

from .actual import ActualAction, ActualActionReader, ActualReadError
from .diff import (
    ActionBucket,
    DiffEntry,
    DiffReport,
    diff_actions,
    generate_report,
    normalize_arguments,
    render_report,
)
from .recorder import IntendedActionRecorder, RecorderError

__all__ = [
    "ActionBucket",
    "ActualAction",
    "ActualActionReader",
    "ActualReadError",
    "DiffEntry",
    "DiffReport",
    "IntendedActionRecorder",
    "RecorderError",
    "diff_actions",
    "generate_report",
    "normalize_arguments",
    "render_report",
]
