"""The host-visible names this acceptance owns, and what it seeds.

Every resource the run takes on the host -- tmux sessions, compose projects,
volumes, and the run's temporary directory -- carries the run's prefix, so a
sweep of this harness can only ever reach its own leftovers. The shared package
takes it as a parameter precisely so that no harness bakes another's name in.

Two runs live here and they must never reach each other: the recreated
publication acceptance the driver walks, and the crash/restart matrix the
runner walks. They provision their own forges, their own databases, and their
own tmux servers, so they carry their own prefixes and their own run ids.
"""

from __future__ import annotations

import secrets
from typing import Any

#: The prefix every host-visible name of the recreated-publication run carries.
PREFIX = "exo-e2e-1117-"

#: The prefix every host-visible name of the crash/restart matrix carries.
MATRIX_PREFIX = "exo-e2e-1057-"

#: The run-directory prefix ``mktemp -d`` uses under the run's temp root.
RUN_DIRECTORY_PREFIX = PREFIX

#: The leaf slice the scenario publishes. Its branch and worktree are derived
#: from this name by the shipped spawn path (``main.<name>-<agent type>``), and
#: the harness reads the names back out of the ledger rather than predicting
#: them.
LEAF_SLICE = "out"

#: The acceptance legs. ``recreate`` is the #1117 scenario unchanged;
#: ``control`` is the same dispatch with no recreate (the #1138 control);
#: ``child`` puts the leaf under a child sub-TL, the #1112 shape (#1138 step 4).
LEGS: tuple[str, ...] = ("recreate", "control", "child")

#: The child sub-TL that owns the leaf in the ``child`` leg.
CHILD_SUB_TL = "stage"

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


def new_run_id() -> str:
    """Return a run id unique to this invocation.

    It becomes the compose project name, the tmux session prefix, the Forgejo
    account and repository names, and part of the temporary directory, so two
    runs can never reach each other's state. The length is bounded because the
    tmux session name is derived from it and the server rejects a session name
    past its limit at config load.
    """
    return secrets.token_hex(4)
