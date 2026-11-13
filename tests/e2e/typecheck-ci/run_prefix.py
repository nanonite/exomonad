"""The host-visible name prefix this acceptance owns.

Every resource the run takes on the host -- its compose project, volumes,
networks, and temporary directory -- carries this prefix, so a sweep or leak
check of this harness can only ever reach its own leftovers and never another
acceptance's. The shared cleanup package takes the prefix as a parameter
precisely so no harness bakes another's name in.
"""

from __future__ import annotations

PREFIX = "exo-e2e-1332-"

#: The run-directory prefix ``mktemp -d`` uses under the run's temp root.
RUN_DIRECTORY_PREFIX = PREFIX
