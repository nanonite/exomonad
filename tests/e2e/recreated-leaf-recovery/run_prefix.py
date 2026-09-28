"""The host-visible name prefix this acceptance owns.

Every resource this run takes on the host -- tmux sessions, compose projects,
volumes, and the run's temporary directory -- carries this prefix, so a sweep
of this harness can only ever reach its own leftovers and can never reach
another acceptance's. The shared package takes it as a parameter precisely so
that no harness bakes another's name in.
"""

from __future__ import annotations

PREFIX = "exo-e2e-1111-"

#: The run-directory prefix ``mktemp -d`` uses under the run's temp root. It is
#: derived from the same harness identity so a stale directory is attributable
#: to this harness alone.
RUN_DIRECTORY_PREFIX = PREFIX
