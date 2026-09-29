"""The host-visible names this acceptance owns, and what it seeds.

Every resource the run takes on the host -- tmux sessions, compose projects,
volumes, and the run's temporary directory -- carries ``PREFIX``, so a sweep of
this harness can only ever reach its own leftovers. The shared package takes it
as a parameter precisely so that no harness bakes another's name in.
"""

from __future__ import annotations

from typing import Any

PREFIX = "exo-e2e-1117-"

#: The run-directory prefix ``mktemp -d`` uses under the run's temp root.
RUN_DIRECTORY_PREFIX = PREFIX

#: The leaf slice the scenario publishes. Its branch and worktree are derived
#: from this name by the shipped spawn path (``main.<name>-<agent type>``), and
#: the harness reads the names back out of the ledger rather than predicting
#: them.
LEAF_SLICE = "out"

#: Every issue the scenario is seeded with. Each is created fresh in the
#: disposable database on every run, so no run depends on another's rows, and
#: no existing database is read or written.
SEED_ISSUES: tuple[dict[str, Any], ...] = (
    {
        "title": "Publish the recreated publication acceptance leaf",
        "priority": "high",
        "labels": ("bug",),
    },
    {
        "title": "Escalate the recreated publication exactly once",
        "priority": "high",
        "labels": ("needs-human",),
    },
)
